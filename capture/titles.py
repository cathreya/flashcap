"""Fill empty reading-item titles from each page's own metadata.

Usage::

    python -m capture.titles
    python -m capture.titles --limit 50 --dry-run
    python -m capture.titles --workers 4 --db /tmp/cards.db

Telegram's JSON export carries no link previews, so an imported link can land
with no title. For most pages this reads ``og:title``, then ``twitter:title``,
then ``<title>``. YouTube and X serve a JavaScript shell to a plain fetch, so
those use each site's oEmbed endpoint instead.

A stored title is never replaced, so the command is safe and cheap to re-run.
This is a one-shot job, not a worker: no request in the app triggers a crawl.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from . import store
from .config import get_db_path

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 10
MAX_BYTES = 512 * 1024
_MAX_LEN = 400
_TWEET_LEN = 140
_TCO = re.compile(r"\s*https?://t\.co/\S+")
_TITLE_META = (
    {"property": "og:title"},
    {"name": "og:title"},
    {"name": "twitter:title"},
    {"property": "twitter:title"},
)
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "youtu.be"}
_TWITTER_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}
_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org"}
_ARXIV_PDF = re.compile(r"^/pdf/(.+?)(?:\.pdf)?/?$", re.IGNORECASE)
_NETWORK_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError)


def fetch_title(url: str, timeout: int = TIMEOUT) -> str | None:
    """Return a page's title, or ``None`` when the page offers nothing usable."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in _YOUTUBE_HOSTS:
        title = _youtube_title(url, timeout)
        if title:
            return title
    elif host in _TWITTER_HOSTS:
        title = _tweet_title(url, timeout)
        if title:
            return title
    elif host in _ARXIV_HOSTS:
        match = _ARXIV_PDF.match(parts.path)
        if match:
            return _page_title(f"https://arxiv.org/abs/{match.group(1)}", timeout)

    return _page_title(url, timeout)


def fill_titles(
    limit: int = 0,
    workers: int = 8,
    dry_run: bool = False,
    timeout: int = TIMEOUT,
) -> dict[str, int]:
    """Fetch and store titles for rows that lack one. Returns tally counts.

    Fetching runs in threads, but every write happens on the calling thread:
    ``store`` holds its connection on Flask's request context, which a worker
    thread would not own.
    """
    items = store.list_reading_items_without_title(limit)
    counts = {"candidates": len(items), "filled": 0, "failed": 0}

    pending = [(item["id"], item["url"]) for item in items]

    def worker(item: tuple[int, str]) -> tuple[int, str | None]:
        item_id, url = item
        try:
            return item_id, fetch_title(url, timeout)
        except Exception:  # noqa: BLE001 - one bad page must not stop the crawl
            return item_id, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for done, (item_id, title) in enumerate(pool.map(worker, pending), start=1):
            if title:
                if not dry_run:
                    store.set_reading_title_if_empty(item_id, title)
                counts["filled"] += 1
            else:
                counts["failed"] += 1
            if done % 25 == 0:
                print(f"  {done}/{len(pending)}…", file=sys.stderr, flush=True)

    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fill missing reading-item titles from page metadata."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Only look at this many rows."
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="Concurrent fetches. Defaults to 8."
    )
    parser.add_argument(
        "--timeout", type=int, default=TIMEOUT, help="Per-request timeout in seconds."
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write.")
    parser.add_argument(
        "--db", default=get_db_path(), help="Path to the Flashcap database."
    )
    args = parser.parse_args(argv)

    from .server import create_app

    app = create_app(args.db)
    with app.app_context():
        counts = fill_titles(args.limit, args.workers, args.dry_run, args.timeout)

    verb = "would fill" if args.dry_run else "filled"
    print(
        f"Checked {counts['candidates']} untitled items, {verb} {counts['filled']}, "
        f"no title on {counts['failed']}."
    )


def _page_title(url: str, timeout: int) -> str | None:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        if content_type and "html" not in content_type.lower():
            return None
        raw = response.read(MAX_BYTES)

    soup = BeautifulSoup(raw, "html.parser")

    for attrs in _TITLE_META:
        meta = soup.find("meta", attrs=attrs)
        content = meta.get("content") if meta else None
        title = _clean(content)
        if title:
            return title

    if soup.title:
        return _clean(soup.title.get_text())
    return None


def _youtube_title(url: str, timeout: int) -> str | None:
    endpoint = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    payload = _oembed(endpoint, timeout)
    return _clean(payload.get("title"))


def _tweet_title(url: str, timeout: int) -> str | None:
    """A tweet has no title, so use the start of the tweet body."""
    endpoint = "https://publish.twitter.com/oembed?url=" + quote(url, safe="")
    payload = _oembed(endpoint, timeout)
    html = str(payload.get("html") or "")
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    paragraph = soup.find("p")
    text = _TCO.sub("", paragraph.get_text(" ", strip=True) if paragraph else "")
    if not text:
        return None
    return text[:_TWEET_LEN].strip()


def _oembed(endpoint: str, timeout: int) -> dict:
    request = Request(endpoint, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except _NETWORK_ERRORS:
        return {}


def _clean(raw: object) -> str | None:
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    return text[:_MAX_LEN]


if __name__ == "__main__":
    main()
