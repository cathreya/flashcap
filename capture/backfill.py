"""Import a Telegram Desktop export into the reading library.

Usage::

    python -m capture.backfill
    python -m capture.backfill --html /path/to/messages.html --db /tmp/cards.db
    python -m capture.backfill --html /path/to/result.json

Both export shapes are accepted: ``messages.html`` and the JSON ``result.json``.
This is the history path. Live messages arrive through ``capture.telegram``.
The import is idempotent: a link already in the library keeps its status.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from . import library, store
from .config import PROJECT_ROOT, get_db_path

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING = ".,;:!?\"')"


def parse_export(path: str) -> list[dict]:
    """Turn one Telegram export into library rows, oldest message first."""
    if Path(path).suffix.lower() == ".json":
        return parse_json_export(path)
    return parse_html_export(path)


def parse_html_export(html_path: str) -> list[dict]:
    """Turn one Telegram HTML export into library rows, oldest message first."""
    with open(html_path, encoding="utf-8") as handle:
        soup = BeautifulSoup(handle.read(), "html.parser")

    items: list[dict] = []
    seen: set[str] = set()

    for message in soup.select("div.message.default.clearfix"):
        message_id = _message_id(message.get("id", ""))
        saved_at = _saved_at(message)
        body = message.find("div", class_="body")
        text_div = body.find("div", class_="text") if body else None
        text = text_div.get_text(" ", strip=True) if text_div else ""

        links: dict[str, dict] = {}
        for anchor in text_div.find_all("a", href=True) if text_div else []:
            href = _clean_url(anchor.get("href", ""))
            key = library.canonical_key(href)
            if key:
                label = anchor.get_text(strip=True)
                links.setdefault(
                    key,
                    {
                        "url": key,
                        "url_key": key,
                        "title": label if label and not label.startswith("http") else None,
                    },
                )

        for raw_url in _URL_RE.findall(text):
            key = library.canonical_key(_clean_url(raw_url))
            if key:
                links.setdefault(
                    key, {"url": key, "url_key": key, "title": None}
                )

        items.extend(_link_rows(links, text, message_id, saved_at, seen))

    return items


def parse_json_export(json_path: str) -> list[dict]:
    """Turn one Telegram Desktop ``result.json`` export into library rows.

    The JSON export carries no link previews, so a bare URL gets no title. The
    only titles it can supply are text_link entities, where the label is the
    text the author typed.
    """
    with open(json_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    items: list[dict] = []
    seen: set[str] = set()

    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("type") != "message":
            continue
        message_id = _message_id(message.get("id"))
        saved_at = _json_saved_at(message)
        text = _json_text(message.get("text"))

        links: dict[str, dict] = {}
        for entity in message.get("text_entities") or []:
            if not isinstance(entity, dict):
                continue
            kind = entity.get("type")
            if kind == "text_link" and entity.get("href"):
                raw_url = str(entity["href"])
                label = str(entity.get("text") or "").strip()
            elif kind == "link":
                raw_url = str(entity.get("text") or "")
                label = ""
            else:
                continue
            key = library.canonical_key(_clean_url(raw_url))
            if key:
                links.setdefault(
                    key,
                    {
                        "url": key,
                        "url_key": key,
                        "title": label if label and not label.startswith("http") else None,
                    },
                )

        for raw_url in _URL_RE.findall(text):
            key = library.canonical_key(_clean_url(raw_url))
            if key:
                links.setdefault(key, {"url": key, "url_key": key, "title": None})

        items.extend(_link_rows(links, text, message_id, saved_at, seen))

    return items


def _link_rows(
    links: dict[str, dict],
    text: str,
    message_id: int,
    saved_at: str,
    seen: set[str],
) -> list[dict]:
    """Turn one message's deduped links, or its leftover text, into rows."""
    rows: list[dict] = []

    for link in links.values():
        if link["url_key"] in seen:
            continue
        seen.add(link["url_key"])
        rows.append(
            {
                "url": link["url"],
                "url_key": link["url_key"],
                "title": link["title"],
                "site": library.host_of(link["url"]),
                "description": _description(text),
                "source": library.SOURCE_EXPORT,
                "message_id": message_id,
                "saved_at": saved_at,
            }
        )

    stripped = text.strip()
    if not links and stripped:
        key = f"export:{message_id}"
        if key not in seen:
            seen.add(key)
            rows.append(
                {
                    "url": None,
                    "url_key": key,
                    "title": None,
                    "site": None,
                    "description": stripped,
                    "source": library.SOURCE_EXPORT,
                    "message_id": message_id,
                    "saved_at": saved_at,
                }
            )

    return rows


def import_export(path: str) -> dict[str, int]:
    """Write the export's rows into the library. Safe to run again."""
    items = parse_export(path)
    before = store.reading_counts()["total"]
    for item in items:
        store.upsert_reading_item(item)
    after = store.reading_counts()["total"]
    return {"seen": len(items), "added": after - before, "total": after}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Load a Telegram export into the reading library."
    )
    parser.add_argument(
        "--html",
        default=str(PROJECT_ROOT / "messages.html"),
        help="Path to messages.html or the JSON result.json.",
    )
    parser.add_argument(
        "--db",
        default=get_db_path(),
        help="Path to the Flashcap database.",
    )
    args = parser.parse_args(argv)

    from .server import create_app

    app = create_app(args.db)
    with app.app_context():
        counts = import_export(args.html)

    print(
        f"Read {counts['seen']} rows, added {counts['added']}, "
        f"library now holds {counts['total']}."
    )


def _message_id(raw: object) -> int:
    digits = re.sub(r"\D", "", str(raw or ""))
    return int(digits) if digits else 0


def _json_text(raw: object) -> str:
    """Flatten Telegram's string-or-parts text field into one string."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for part in raw:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            parts.append(str(part.get("text") or ""))
    return "".join(parts)


def _json_saved_at(message: dict) -> str:
    """``date_unixtime`` is UTC; the human ``date`` field is the author's zone."""
    raw = message.get("date_unixtime")
    if isinstance(raw, (int, str)) and not isinstance(raw, bool):
        try:
            moment = datetime.fromtimestamp(int(raw), tz=timezone.utc)
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError):
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _saved_at(message) -> str:
    date = message.find("div", class_="date")
    raw = str(date.get("title") if date else "").strip()
    if raw:
        try:
            moment = datetime.strptime(
                raw.replace("UTC", "").strip(), "%d.%m.%Y %H:%M:%S %z"
            )
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_url(raw: object) -> str:
    return str(raw or "").strip().rstrip(_TRAILING)


def _description(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    without_links = _URL_RE.sub("", stripped).strip(" -:;|")
    return stripped if without_links else None


if __name__ == "__main__":
    main()
