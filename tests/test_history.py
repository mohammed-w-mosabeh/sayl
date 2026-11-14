"""History export and import: no secrets out, nothing started on the way in."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from daemon import history, stats
from daemon.models import Category, Download, EngineName, Status
from tests.stack import Stack, running_stack


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        yield running


def doc(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"format": "sayl-history", "version": 1, "downloads": list(entries)}


async def test_export_has_no_headers_and_import_round_trips(tmp_path: Path) -> None:
    async with running_stack(tmp_path / "a") as first:
        services = first.daemon.services
        video = next(c for c in services.store.all(Category) if c.name == "Video")
        services.store.add(
            Download(
                id="done1",
                url="https://a.example/f.mp4",
                filename="f.mp4",
                status=Status.COMPLETED,
                total_bytes=10,
                category_id=video.id,
                headers_enc=services.vault.seal({"Cookie": "sid=secret"}),
                finished_at=datetime(2026, 11, 1, tzinfo=UTC),
            )
        )
        exported = (await first.client.get("/history/export")).json()
    assert "secret" not in str(exported)
    assert "headers_enc" not in exported["downloads"][0]
    assert exported["downloads"][0]["category"] == "Video"
    async with running_stack(tmp_path / "b") as second:
        result = await second.client.post("/history/import", json=exported)
        assert result.json() == {"added": 1, "skipped": 0}
        again = await second.client.post("/history/import", json=exported)
        assert again.json() == {"added": 0, "skipped": 1}
        row = second.daemon.services.store.download("done1")
        assert row is not None
        assert (row.status, row.filename, row.total_bytes) == (Status.COMPLETED, "f.mp4", 10)
        categories = {c.id: c.name for c in second.daemon.services.store.all(Category)}
        assert categories[row.category_id or ""] == "Video"


async def test_import_never_starts_anything(stack: Stack) -> None:
    result = await stack.client.post(
        "/history/import",
        json=doc(
            {"id": "q", "url": "https://a.example/q.bin", "status": "active"},
            {"id": "c", "url": "https://a.example/c.bin", "status": "queued", "engine": "hybrid"},
            {"url": "https://a.example/new.bin", "filename": "../../.bashrc"},
        ),
    )
    assert result.json()["added"] == 3
    store = stack.daemon.services.store
    paused, capture = store.download("q"), store.download("c")
    assert paused is not None and paused.status is Status.PAUSED
    assert capture is not None and capture.status is Status.NEEDS_REFRESH
    names = [row.filename for row in store.downloads() if row.url.endswith("new.bin")]
    assert names == ["bashrc"]  # Sanitized: no path, no hidden dot file.
    assert not stack.daemon.services.queue.active


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"format": "other", "version": 1, "downloads": []},
        {"format": "sayl-history", "version": 2, "downloads": []},
        doc({"status": "completed"}),
        doc({"url": "https://a", "status": "exploded"}),
        doc({"url": "https://a", "total_bytes": -1}),
        doc({"url": "https://a", "created_at": "yesterday"}),
        doc("not an object"),  # type: ignore[arg-type]
    ],
)
def test_bad_files_are_refused(document: Any) -> None:
    with pytest.raises(history.HistoryError):
        history.parse(document, [])


async def test_bad_import_is_a_422(stack: Stack) -> None:
    response = await stack.client.post("/history/import", json={"format": "nope"})
    assert response.status_code == 422


def test_stats_count_bytes_by_day_host_and_category() -> None:
    music = Category(id="m", name="Music")
    rows = [
        Download(
            url="https://a.example/1",
            status=Status.COMPLETED,
            total_bytes=1000,
            category_id="m",
            started_at=datetime(2026, 11, 14, 10, 0, 0),
            finished_at=datetime(2026, 11, 14, 10, 0, 10),
        ),
        Download(
            url="https://b.example/2",
            final_url="https://cdn.example/2",
            status=Status.COMPLETED,
            done_bytes=500,
            finished_at=datetime(2026, 11, 1, tzinfo=UTC),
        ),
        Download(
            url="https://a.example/old",
            status=Status.COMPLETED,
            total_bytes=7,
            finished_at=datetime(2025, 1, 1),
        ),
        Download(url="https://a.example/3", status=Status.FAILED, total_bytes=99),
        Download(url="https://a.example/4", engine=EngineName.YTDLP),
    ]
    result = stats.compute(rows, [music], date(2026, 11, 14))
    assert result["by_status"] == {"completed": 3, "failed": 1, "queued": 1}
    assert result["completed"] == 3
    assert result["completed_bytes"] == 1507
    days = {item["day"]: item["bytes"] for item in result["per_day"]}
    assert len(days) == 30
    assert days["2026-11-14"] == 1000 and days["2026-11-01"] == 500
    assert "2025-01-01" not in days
    assert result["top_hosts"][0] == {"host": "a.example", "bytes": 1007}
    assert {"host": "cdn.example", "bytes": 500} in result["top_hosts"]
    assert {"category": "Music", "bytes": 1000} in result["by_category"]
    assert result["average_speed"] == 100


async def test_stats_endpoint(stack: Stack) -> None:
    body = (await stack.client.get("/stats")).json()
    assert body["completed"] == 0
    assert len(body["per_day"]) == 30
    assert body["average_speed"] == 0


async def test_save_writes_the_export_to_a_chosen_file(stack: Stack, tmp_path: Path) -> None:
    target = tmp_path / "sayl-history.json"
    response = await stack.client.post("/history/save", json={"path": str(target)})
    assert response.status_code == 204
    assert history.parse(json.loads(target.read_text()), []) == []
    for bad in ("relative.json", str(tmp_path / "x.txt"), str(tmp_path / "missing" / "x.json")):
        refused = await stack.client.post("/history/save", json={"path": bad})
        assert refused.status_code == 422
