#!/usr/bin/env python3
"""
Instagram Saved Posts → Notion sync daemon.
Runs twice a day via launchd. Uses browser session cookies — no password stored.
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

# ── Fetch collections ─────────────────────────────────────────────────────────

def fetch_collections(session: requests.Session, target_collections: list) -> list:
    """Return [{id, name}] for named collections matching target_collections (or all if empty)."""
    r = session.get(
        "https://www.instagram.com/api/v1/collections/list/",
        params={"collection_types": '["MEDIA"]'},
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    result = []
    for item in items:
        name = item.get("collection_name") or "All Posts"
        cid  = item["collection_id"]
        if not target_collections or name in target_collections:
            result.append({"id": cid, "name": name})
    return result

# ── Fetch ALL saved posts (no collection filter) ──────────────────────────────

def fetch_all_saved(session: requests.Session) -> list:
    """Fetch every saved post regardless of collection using the main saved feed."""
    posts, next_max_id = [], None
    while True:
        params = {}
        if next_max_id:
            params["max_id"] = next_max_id
        r = session.get(
            "https://www.instagram.com/api/v1/feed/saved/posts/",
            params=params,
            timeout=15,
        )
        if r.status_code == 403:
            log.warning("403 on saved feed — cookies may have expired.")
            break
        r.raise_for_status()
        data  = r.json()
        items = data.get("items", [])
        for item in items:
            posts.append(item.get("media", item))
        if not data.get("more_available") or not items:
            break
        next_max_id = data.get("next_max_id")
        time.sleep(0.4)
    return posts

# ── Fetch posts in a named collection ─────────────────────────────────────────

def fetch_posts(session: requests.Session, collection_id: str) -> list:
    posts, next_max_id = [], None
    while True:
        params = {}
        if next_max_id:
            params["max_id"] = next_max_id
        r = session.get(
            f"https://www.instagram.com/api/v1/feed/collection/{collection_id}/posts/",
            params=params,
            timeout=15,
        )
        if r.status_code == 403:
            log.warning("403 on collection %s — cookies may have expired.", collection_id)
            break
        r.raise_for_status()
        data  = r.json()
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
        "name":         f"@{author} — {caption[:80]}{'…' if len(caption) > 80 else ''}",
    }

# ── Notion writer ─────────────────────────────────────────────────────────────

def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }

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
            "Caption":    {"rich_text": [{"text": {"content": (post["caption"][:1997] + "...") if len(post["caption"]) > 2000 else post["caption"]}}]},
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

    # Cookies come from instagrapi, which logs in and re-logs in automatically
    # (credentials in the macOS Keychain — see auth.py / setup_auth.py). If that
    # is not set up yet, fall back to the manual cookie in config.json so the
    # pipeline keeps working during the transition.
    try:
        from auth import get_ig_cookies
        cookies = get_ig_cookies()
        log.info("Auth via instagrapi (auto-refreshing session).")
    except Exception as e:
        log.warning("instagrapi auth unavailable (%s) — using config.json cookie.", e)
        cookies = cfg.get("instagram_cookies", {})
        if not cookies.get("sessionid"):
            log.error("No usable Instagram credentials. Run: .venv/bin/python setup_auth.py")
            sys.exit(1)

    synced_ids = load_state()
    session    = make_session(cookies)

    # Build a collection lookup: media_id → collection_name
    collection_map = {}
    try:
        collections = fetch_collections(session, target_cols)
        log.info("Named collections: %s", [c["name"] for c in collections])
        for col in collections:
            try:
                col_posts = fetch_posts(session, col["id"])
                for media in col_posts:
                    mid = str(media.get("id", ""))
                    if mid:
                        collection_map[mid] = col["name"]
            except Exception as e:
                log.warning("Skipping collection %s: %s", col["name"], e)
    except Exception as e:
        log.warning("Could not fetch collections: %s", e)

    # Fetch ALL saved posts via the main saved feed
    log.info("Fetching all saved posts...")
    try:
        all_posts = fetch_all_saved(session)
        log.info("Total saved posts found: %d", len(all_posts))
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
