import json
import logging
from pathlib import Path

from daemon.logs import JsonFormatter, redact_text, redact_url, setup_logging


def format_record(message: str, **fields: object) -> dict[str, object]:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    record.fields = fields
    result: dict[str, object] = json.loads(JsonFormatter().format(record))
    return result


def test_url_keeps_only_host_and_path() -> None:
    assert redact_url("https://u:p@a.io:8/f.zip?sig=1#x") == "https://a.io:8/f.zip"


def test_text_hides_tokens_cookies_and_queries() -> None:
    text = redact_text("got Bearer abc Cookie: sid=1 from https://a.io/f?token=2")
    assert "abc" not in text
    assert "sid=1" not in text
    assert "token=2" not in text


def test_formatter_redacts_fields_recursively() -> None:
    entry = format_record(
        "x", headers={"a": "b"}, nested={"Cookie": "c", "url": "http://a.io/?s=1"}, items=["ok"]
    )
    assert entry["headers"] == "[redacted]"
    assert entry["nested"] == {"Cookie": "[redacted]", "url": "http://a.io/"}
    assert entry["items"] == ["ok"]


def test_exception_text_is_redacted() -> None:
    try:
        raise ValueError("Authorization: secret")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    assert "secret" not in JsonFormatter().format(record)


def test_setup_logging_writes_json(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        setup_logging(tmp_path / "x.log")
        logging.getLogger("t").info("hello")
        assert json.loads((tmp_path / "x.log").read_text())["message"] == "hello"
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
