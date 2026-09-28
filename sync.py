#!/usr/bin/env python3
"""
Instagram Saved Posts → Notion sync daemon.
Runs twice a day via launchd. Uses browser session cookies : no password stored.
"""

import json
import os
import sys
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE   = BASE_DIR / "sync.log"
CONFIG_FILE = BASE_DIR / "config.json"

NOTION_API  = "https://api.notion.com/v1"
NOTION_VER  = "2022-06-28"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config loader ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.json not found. Copy config.example.json → config.json and fill in your values.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())

# ── State (dedup) ─────────────────────────────────────────────────────────────

def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()).get("synced_ids", []))
    return set()

def save_state(ids: set):
    STATE_FILE.write_text(json.dumps({"synced_ids": sorted(ids)}, indent=2))

# ── Instagram session ─────────────────────────────────────────────────────────

class SessionExpired(Exception):
    """The Instagram session is no longer accepted (403 / login required)."""


def make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    })
    s.cookies.update(cookies)
    if csrf := cookies.get("csrftoken"):
        s.headers["X-CSRFToken"] = csrf
    return s

# ── Transports ────────────────────────────────────────────────────────────────
# Both return fetch(endpoint, params) -> parsed JSON dict, so the fetch helpers
# below don't care which one they got.
#
# Why two: we log in with instagrapi, which is an Android app client. Replaying
# those cookies through a desktop Chrome User-Agent : which is what this script
# used to do : makes one session appear to be used from a phone and from a
# desktop browser alternately. Instagram reads that as a stolen session: it
# kills the session (403), the next run has to send a real password login, and
# that login tends to draw a challenge. Symptom is a sync that works for a day
# or two, then breaks, then works again. Going through the mobile API keeps a
# single coherent device for both login and reads, which is what the session was
# issued for. The web transport stays only as the fallback for the manual
# config.json cookie, which is a real browser cookie anyway.

def make_mobile_fetch(cl):
    """Read through instagrapi's own signed mobile API (same device as login)."""
    from instagrapi.exceptions import (
        ClientForbiddenError, ClientLoginRequired, ClientUnauthorizedError,
        LoginRequired,
    )
    refused = (LoginRequired, ClientLoginRequired,
               ClientForbiddenError, ClientUnauthorizedError)

    def fetch(endpoint: str, params: dict | None = None) -> dict:
        try:
            return cl.private_request(endpoint, params=params or {})
        except refused as e:
            raise SessionExpired(f"{type(e).__name__}: {e}") from e
    return fetch


def make_web_fetch(session: requests.Session):
    """Read through the public web API using a browser cookie."""
    def fetch(endpoint: str, params: dict | None = None) -> dict:
        r = session.get(
            f"https://www.instagram.com/api/v1/{endpoint}",
            params=params or {},
            timeout=15,
        )
        if r.status_code == 403:
            raise SessionExpired("403 from the web API")
        r.raise_for_status()
        return r.json()
    return fetch

# ── Fetch collections ─────────────────────────────────────────────────────────

def fetch_collections(fetch, target_collections: list) -> list:
    """Return [{id, name}] for named collections matching target_collections (or all if empty)."""
    data = fetch("collections/list/", {"collection_types": '["MEDIA"]'})
    items = data.get("items", [])
    result = []
    for item in items:
        name = item.get("collection_name") or "All Posts"
        cid  = item["collection_id"]
        if not target_collections or name in target_collections:
            result.append({"id": cid, "name": name})
    return result

# ── Fetch ALL saved posts (no collection filter) ──────────────────────────────

def fetch_all_saved(fetch, known_ids: set | None = None) -> list:
    """Fetch saved posts from the main saved feed, newest first.

    The feed is ordered newest-first, so the first page that contains nothing
    but already-synced posts means everything below it is older and already in
    Notion. Stopping there turns a ~50-request crawl of the whole collection
    into one or two requests on a normal day. That matters beyond speed: a full
    crawl on every scheduled run is what pushes Instagram into 403ing the
    session, which then forces a password login and fires a security alert each
    time. Pass known_ids=None to force a full crawl (backfill/repair).
    """
    posts, next_max_id, page = [], None, 0
    while True:
        params = {}
        if next_max_id:
            params["max_id"] = next_max_id
        try:
            data = fetch("feed/saved/posts/", params)
        except SessionExpired as e:
            log.warning("Saved feed refused the session (%s) : stopping here.", e)
            break
        items = data.get("items", [])
        page += 1
        page_posts = [item.get("media", item) for item in items]
        posts.extend(page_posts)

        if known_ids is not None and page_posts and all(
            str(p.get("id", "")) in known_ids for p in page_posts
        ):
            log.info(
                "Page %d already fully synced : stopping early (%d posts scanned).",
                page, len(posts),
            )
            break
        if not data.get("more_available") or not items:
            break
        next_max_id = data.get("next_max_id")
        time.sleep(0.4)
    return posts

# ── Fetch posts in a named collection ─────────────────────────────────────────

def fetch_posts(fetch, collection_id: str) -> list:
    posts, next_max_id = [], None
    while True:
        params = {}
        if next_max_id:
            params["max_id"] = next_max_id
        try:
            data = fetch(f"feed/collection/{collection_id}/posts/", params)
        except SessionExpired as e:
            log.warning("Collection %s refused the session (%s).", collection_id, e)
            break
        items = data.get("items", [])
        for item in items:
            posts.append(item.get("media", item))
        if not data.get("more_available") or not items:
            break
        next_max_id = data.get("next_max_id")
        time.sleep(0.4)
    return posts

# ── Parse a media object ──────────────────────────────────────────────────────

TYPE_MAP = {1: "Post", 2: "Reel", 8: "Carousel"}

def parse_post(media: dict, collection_name: str) -> dict:
    media_type   = media.get("media_type", 1)
    content_type = TYPE_MAP.get(media_type, "Post")
    if content_type == "Reel" and media.get("product_type") == "igtv":
        content_type = "IGTV"

    user    = media.get("user", {})
    author  = user.get("username", "")
    caption_obj = media.get("caption")
    caption = (caption_obj.get("text", "") if isinstance(caption_obj, dict) else caption_obj or "")
    media_id = str(media.get("id", ""))
    code     = media.get("code") or media.get("shortcode", "")
    url      = f"https://www.instagram.com/p/{code}/" if code else ""
    taken_at = media.get("taken_at")
    saved_at = (
        datetime.fromtimestamp(taken_at, tz=timezone.utc).isoformat()
        if taken_at else datetime.now(timezone.utc).isoformat()
    )

    # Extract thumbnail URL (works for images, carousels, and reels)
    thumbnail_url = (
        media.get("thumbnail_url")
        or media.get("cover_media", {}).get("cropped_image_version", {}).get("url")
    )
    if not thumbnail_url:
        candidates = (
            media.get("image_versions2", {}).get("candidates", [])
            or (media.get("carousel_media", [{}])[0]
                .get("image_versions2", {}).get("candidates", []))
        )
        if candidates:
            # Pick a medium-sized image (not the tiny one, not the 4K one)
            sorted_c = sorted(candidates, key=lambda c: c.get("width", 0))
            mid = sorted_c[len(sorted_c) // 2]
            thumbnail_url = mid.get("url", "")

    return {
        "media_id":     media_id,
        "author":       author,
        "caption":      caption[:1990],
        "url":          url,
        "type":         content_type,
        "collection":   collection_name,
        "saved_at":     saved_at[:10],   # date only
        "thumbnail_url": thumbnail_url or "",
        "name":         f"@{author} : {caption[:80]}{'…' if len(caption) > 80 else ''}",
    }

# ── Notion writer ─────────────────────────────────────────────────────────────

def clip_2000(s: str) -> str:
    """Clip to Notion's 2000-char rich_text limit, counted in UTF-16 units.
    Notion measures UTF-16 code units, so an emoji outside the BMP counts as 2,
    not 1. A naive character slice can still overflow the limit and get a 400."""
    if len(s.encode("utf-16-le")) // 2 <= 2000:
        return s
    out, n = [], 0
    for ch in s:
        w = len(ch.encode("utf-16-le")) // 2
        if n + w > 1997:
            break
        out.append(ch)
        n += w
    return "".join(out) + "..."


def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def fetch_notion_identities(token: str, db_id: str) -> dict:
    """Return the URLs and media IDs already stored in the saves database."""
    identities = {"urls": set(), "media_ids": set()}
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        response = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=notion_headers(token), json=body, timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        for page in data.get("results", []):
            properties = page.get("properties", {})
            url = properties.get("URL", {}).get("url") or ""
            if url:
                identities["urls"].add(url)
            media_id = "".join(
                item.get("plain_text") or item.get("text", {}).get("content", "")
                for item in properties.get("Media ID", {}).get("rich_text", [])
            )
            if media_id:
                identities["media_ids"].add(media_id)
        if not data.get("has_more"):
            return identities
        cursor = data.get("next_cursor")
        time.sleep(0.2)

def create_notion_page(token: str, db_id: str, post: dict) -> bool:
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "Name":       {"title":     [{"text": {"content": post["name"]}}]},
            "URL":        {"url":        post["url"] or None},
            "Type":       {"select":    {"name": post["type"]}},
            "Author":     {"rich_text": [{"text": {"content": post["author"]}}]},
            "Status":     {"select":    {"name": "New"}},
            "Media ID":   {"rich_text": [{"text": {"content": post["media_id"]}}]},
            "Saved":      {"date":      {"start": post["saved_at"]}},
            "Caption":    {"rich_text": [{"text": {"content": clip_2000(post["caption"])}}]},
            "Collection": {"select":    {"name": post["collection"]}},
        },
    }
    # Set thumbnail as page cover if available
    if post.get("thumbnail_url"):
        payload["cover"] = {"type": "external", "external": {"url": post["thumbnail_url"]}}
    r = requests.post(f"{NOTION_API}/pages", headers=notion_headers(token), json=payload, timeout=15)
    if r.status_code == 200:
        return True
    log.error("Notion error %s: %s", r.status_code, r.text[:300])
    return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg             = load_config()
    token: str      = cfg["notion_token"]
    db_id: str      = cfg["instagram_saves_db_id"]
    target_cols     = cfg.get("instagram_collections", [])

    log.info("▶ Instagram → Notion sync starting")

    # Prefer the managed mobile session. It has one stable Instagram device
    # fingerprint, unlike the old browser-cookie route. Once it is configured,
    # never retry config.json after an auth error: a stale web cookie adds
    # requests while Instagram is already challenging the account. The manual
    # cookie remains for first-time setups without Keychain credentials.
    from auth import credentials_are_configured, get_authenticated_client
    if credentials_are_configured():
        try:
            fetch = make_mobile_fetch(get_authenticated_client())
            log.info("Auth via instagrapi (mobile API, auto-refreshing session).")
        except Exception as e:
            log.error(
                "Managed Instagram auth unavailable (%s). Skipping this run; "
                "the legacy config.json cookie will not be retried.", e,
            )
            sys.exit(1)
    else:
        log.info("Managed Instagram auth is not configured : using config.json cookie.")
        cookies = cfg.get("instagram_cookies", {})
        if not cookies.get("sessionid"):
            log.error("No usable Instagram credentials. Run: .venv/bin/python setup_auth.py")
            sys.exit(1)
        fetch = make_web_fetch(make_session(cookies))

    synced_ids = load_state()

    # Build a collection lookup: media_id → collection_name
    collection_map = {}
    try:
        collections = fetch_collections(fetch, target_cols)
        log.info("Named collections: %s", [c["name"] for c in collections])
        for col in collections:
            try:
                col_posts = fetch_posts(fetch, col["id"])
                for media in col_posts:
                    mid = str(media.get("id", ""))
                    if mid:
                        collection_map[mid] = col["name"]
            except Exception as e:
                log.warning("Skipping collection %s: %s", col["name"], e)
    except Exception as e:
        log.warning("Could not fetch collections: %s", e)

    # Fetch saved posts via the main saved feed. Normal runs stop as soon as
    # they reach already-synced posts; `--full` re-crawls everything.
    full_crawl = "--full" in sys.argv
    log.info("Fetching saved posts%s...", " (full crawl)" if full_crawl else "")
    try:
        all_posts = fetch_all_saved(fetch, None if full_crawl else synced_ids)
        log.info("Saved posts scanned: %d", len(all_posts))
    except Exception as e:
        log.error("Failed to fetch saved posts: %s", e)
        sys.exit(1)

    new_count = 0
    for media in all_posts:
        mid = str(media.get("id", ""))
        col_name = collection_map.get(mid, "All Posts")
        post = parse_post(media, col_name)
        if not post["media_id"] or post["media_id"] in synced_ids:
            continue
        if create_notion_page(token, db_id, post):
            synced_ids.add(post["media_id"])
            new_count += 1
            log.info("  ✓ %s", post["name"][:60])
            time.sleep(0.3)

    save_state(synced_ids)
    log.info("Done. %d new posts synced. %d total tracked.", new_count, len(synced_ids))

if __name__ == "__main__":
    main()
