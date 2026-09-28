#!/usr/bin/env python3
"""Import Instagram and TikTok links shared from an iPhone text inbox."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import sync


BASE_DIR = Path(__file__).resolve().parent
PROCESSED_LOG = BASE_DIR / "inbox-processed.log"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
IG_RE = re.compile(r"^/(reel|p|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
TIKTOK_RE = re.compile(r"^/@([^/]+)/video/(\d+)", re.IGNORECASE)


def extract_url(line: str) -> str:
    match = URL_RE.search(line)
    return match.group(0).rstrip(".,;:!?)]}") if match else ""


def normalize_url(url: str) -> tuple[str, str]:
    """Return a canonical URL and its platform, or two empty strings."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "", ""
    host = (parsed.hostname or "").lower()
    if host in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        match = IG_RE.match(parsed.path)
        if not match:
            return "", ""
        kind, code = match.groups()
        return f"https://www.instagram.com/{kind.lower()}/{code}/", "instagram"
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        code = parsed.path.strip("/").split("/")[0]
        return (f"https://{host}/{code}/", "tiktok") if code else ("", "")
    if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        match = TIKTOK_RE.match(parsed.path)
        if not match:
            return "", ""
        author, video_id = match.groups()
        return f"https://www.tiktok.com/@{author}/video/{video_id}", "tiktok"
    return "", ""


def url_keys(url: str) -> set[str]:
    normalized, platform = normalize_url(url)
    if not normalized:
        return {url}
    keys = {normalized}
    path = urlsplit(normalized).path
    if platform == "instagram":
        match = IG_RE.match(path)
        if match:
            keys.add(f"ig:{match.group(2)}")
    elif platform == "tiktok":
        match = TIKTOK_RE.match(path)
        if match:
            keys.add(f"tt:{match.group(2)}")
    return keys


def metadata(url: str) -> dict:
    binary = shutil.which("yt-dlp")
    if not binary:
        raise RuntimeError("yt-dlp is not installed. Run: brew install yt-dlp")
    result = subprocess.run(
        [binary, "--dump-json", "--skip-download", "--no-warnings", url],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode:
        details = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(details[-1][:240] if details else f"yt-dlp exit {result.returncode}")
    objects = []
    for line in result.stdout.splitlines():
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not objects:
        raise RuntimeError("yt-dlp returned no metadata")
    primary = dict(objects[0])
    if len(objects) > 1:
        primary["_inbox_entries"] = objects
    return primary


def metadata_date(data: dict) -> str:
    raw = str(data.get("upload_date") or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    timestamp = data.get("timestamp") or data.get("release_timestamp")
    if timestamp:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return datetime.now(timezone.utc).date().isoformat()


def post_from_metadata(data: dict, url: str, platform: str) -> dict:
    author = str(
        data.get("uploader") or data.get("channel") or
        data.get("uploader_id") or data.get("channel_id") or ""
    ).lstrip("@")
    caption = str(data.get("description") or data.get("title") or "")
    media_id = str(data.get("id") or data.get("display_id") or "")
    thumbnail = str(data.get("thumbnail") or "")
    if not thumbnail:
        thumbnails = data.get("thumbnails") or []
        thumbnail = next(
            (str(item.get("url")) for item in reversed(thumbnails) if item.get("url")), ""
        )

    path = urlsplit(url).path
    if platform == "tiktok":
        content_type = "Reel"
        match = TIKTOK_RE.match(path)
        media_id = media_id or (match.group(2) if match else "")
        code = ""
    else:
        match = IG_RE.match(path)
        kind, code = match.groups() if match else ("p", "")
        entries = data.get("_inbox_entries") or data.get("entries") or []
        content_type = "Reel" if kind.lower() in {"reel", "tv"} else (
            "Carousel" if len(entries) > 1 else "Post"
        )
        media_id = media_id or code

    media_type = {"Post": 1, "Reel": 2, "Carousel": 8}.get(content_type, 2)
    post = sync.parse_post({
        "id": media_id,
        "media_type": media_type,
        "code": code,
        "user": {"username": author},
        "caption": {"text": caption},
        "thumbnail_url": thumbnail,
    }, "iPhone Share")
    post.update({
        "url": url,
        "type": content_type,
        "saved_at": metadata_date(data),
        "source": "iPhone share",
    })
    return post


def atomic_write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if lines:
                handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def append_log(entries: list[tuple[str, str]]) -> None:
    if not entries:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with PROCESSED_LOG.open("a", encoding="utf-8") as handle:
        for status, line in entries:
            clean = line.replace("\t", " ").replace("\n", " ").strip()
            handle.write(f"{now}\t{status}\t{clean}\n")


def process(inbox: Path, limit: int, dry_run: bool) -> int:
    if not inbox.exists():
        print(f"Inbox not found: {inbox}")
        return 0
    lines = inbox.read_text(encoding="utf-8", errors="replace").splitlines()
    config = sync.load_config()
    identities = sync.fetch_notion_identities(
        config["notion_token"], config["instagram_saves_db_id"]
    )
    notion_keys = set(identities["media_ids"])
    for existing in identities["urls"]:
        notion_keys.update(url_keys(existing))

    file_keys: set[str] = set()
    remaining: list[str] = []
    journal: list[tuple[str, str]] = []
    seen_urls = created = duplicates = ignored = errors = 0

    for position, line in enumerate(lines):
        raw_url = extract_url(line)
        if not raw_url:
            if line.strip():
                journal.append(("ignored no URL", line))
                ignored += 1
            continue
        if seen_urls >= limit:
            remaining.extend(lines[position:])
            break
        if seen_urls:
            time.sleep(3)
        seen_urls += 1
        url, platform = normalize_url(raw_url)
        if not url:
            journal.append(("ignored unsupported URL", line))
            ignored += 1
            continue
        keys = url_keys(url)
        if keys & file_keys or keys & notion_keys:
            journal.append(("duplicate", line))
            duplicates += 1
            continue
        file_keys.update(keys)
        try:
            post = post_from_metadata(metadata(url), url, platform)
            media_id = post["media_id"]
            if media_id and media_id in notion_keys:
                journal.append(("duplicate media ID", line))
                duplicates += 1
                continue
            if dry_run:
                print(f"DRY RUN: {post['type']} @{post['author']} {post['url']}")
            elif not sync.create_notion_page(
                config["notion_token"], config["instagram_saves_db_id"], post
            ):
                raise RuntimeError("Notion rejected the page")
            else:
                print(f"Created: {post['type']} @{post['author']} {post['url']}")
            journal.append(("dry-run" if dry_run else "created", line))
            notion_keys.update(keys)
            if media_id:
                notion_keys.add(media_id)
            created += 1
        except Exception as error:
            print(f"Failed {url}: {error}")
            remaining.append(line)
            errors += 1

    print(
        f"Result: {created} ready, {duplicates} duplicate(s), "
        f"{ignored} ignored, {errors} failed"
    )
    if dry_run:
        print("Dry run: the inbox and Notion were not modified.")
        return 0
    append_log(journal)
    atomic_write(inbox, remaining)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be greater than zero")
    config = sync.load_config()
    configured = str(config.get("iphone_inbox_path") or "").strip()
    inbox = args.file or (Path(configured).expanduser() if configured else None)
    if inbox is None:
        parser.error("set iphone_inbox_path in config.json or pass --file")
    return process(inbox.expanduser(), args.limit, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
