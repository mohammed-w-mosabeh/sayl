"""Text the daemon shows people: notification titles and buttons. Catalogs are JSON files."""

import json
from functools import cache
from pathlib import Path

LANGUAGES = ("en", "ar")


@cache
def catalog(language: str) -> dict[str, str]:
    file = Path(__file__).with_name(f"{language}.json")
    data: dict[str, str] = json.loads(file.read_text(encoding="utf-8"))
    return data


def language_of(settings: object) -> str:
    """The app's language from the daemon's `ui` setting; English otherwise."""
    ui = settings.get("ui") if isinstance(settings, dict) else None
    value = ui.get("language") if isinstance(ui, dict) else None
    return value if value in LANGUAGES else "en"


def t(language: object, key: str, **values: object) -> str:
    chosen = language if language in LANGUAGES else "en"
    text = catalog(str(chosen)).get(key) or catalog("en")[key]
    return text.format(**values) if values else text
