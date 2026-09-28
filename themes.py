#!/usr/bin/env python3
"""Deduce recurring themes in a catch-all category with local Ollama."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
CACHE_FILE = BASE_DIR / "themes-cache.json"
REPORT_FILE = BASE_DIR / "themes-proposed.md"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
OLLAMA_TAGS = "http://localhost:11434/api/tags"
OLLAMA_GENERATE = "http://localhost:11434/api/generate"
URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
MAX_TEXT = 6000
BATCH_SIZE = 15


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def clean_long_dashes(text: str) -> str:
    return text.replace("\u2014", ":").replace("\u2013", "-")


def rich_text(value: dict) -> str:
    parts = value.get("rich_text") or value.get("title") or []
    return "".join(
        item.get("plain_text", item.get("text", {}).get("content", ""))
        for item in parts
    )


def block_text(block: dict) -> str:
    return rich_text(block.get(block.get("type", ""), {}))


class NotionReader:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, **kwargs) -> dict:
        last = None
        for attempt in range(5):
            try:
                response = requests.request(
                    method, f"{NOTION_API}/{path.lstrip('/')}",
                    headers=self.headers, timeout=35, **kwargs,
                )
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
                continue
            last = response
            if response.status_code == 429:
                time.sleep(float(response.headers.get("Retry-After", 2)))
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        assert last is not None
        last.raise_for_status()
        return {}

    def pages(self, database_id: str, category: str, limit: int) -> list[dict]:
        pages = []
        cursor = None
        prefix = f"[{category}"
        while True:
            size = min(100, limit - len(pages)) if limit else 100
            body: dict = {
                "page_size": size,
                "filter": {"property": "Name", "title": {"starts_with": prefix}},
            }
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"databases/{database_id}/query", json=body)
            pages.extend(data.get("results", []))
            if (limit and len(pages) >= limit) or not data.get("has_more"):
                return pages[:limit] if limit else pages
            cursor = data.get("next_cursor")
            time.sleep(0.2)

    def blocks(self, block_id: str) -> list[dict]:
        blocks = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"blocks/{block_id}/children", params=params)
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                return blocks
            cursor = data.get("next_cursor")


def compact(text: str, maximum: int = MAX_TEXT) -> str:
    clean = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    if len(clean) <= maximum:
        return clean
    return clean[:maximum].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def page_record(reader: NotionReader, page: dict, category: str) -> dict:
    properties = page.get("properties", {})
    full_title = rich_text(properties.get("Name", {}))
    title = re.sub(rf"^\s*\[{re.escape(category)}[^\]]*\]\s*", "", full_title, flags=re.I)
    body_parts = []
    media_parts = []
    source_url = ""
    for block in reader.blocks(page["id"]):
        text = block_text(block)
        if not source_url:
            match = URL_RE.search(text)
            if match:
                source_url = match.group(0).split("?")[0].rstrip("/.,")
        if block.get("type") == "paragraph" and text:
            body_parts.append(text)
        if block.get("type") == "toggle" and text:
            for child in reader.blocks(block["id"]):
                child_text = block_text(child)
                if child_text:
                    media_parts.append(child_text)
    angle = rich_text(properties.get("Angle", {}))
    pieces = [f"Title: {title or 'Untitled'}"]
    if body_parts:
        pieces.append("Caption: " + "\n".join(body_parts))
    if angle:
        pieces.append("Extraction: " + angle)
    if media_parts:
        pieces.append("Media reading: " + "\n".join(media_parts))
    text = compact("\n\n".join(pieces))
    return {
        "page_id": page["id"],
        "title": title or "Untitled",
        "url": source_url or page.get("url", ""),
        "text": text,
        "themes": [],
    }


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_theme(value: object) -> str:
    if not isinstance(value, str):
        return ""
    clean = clean_long_dashes(re.sub(r"\s+", " ", value)).strip(" .,:;#*-\t\n").lower()
    if clean in {"", "none", "null", "n/a", "aucun", "aucune"}:
        return ""
    return clean[:60].rstrip()


def theme_key(theme: str) -> str:
    plain = "".join(
        char for char in unicodedata.normalize("NFKD", theme)
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def extract_json(raw: str):
    cleaned = re.sub(r"```(?:json)?\s*|```", "", raw, flags=re.I).strip()
    decoder = json.JSONDecoder()
    for position, character in enumerate(cleaned):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[position:])
            return value
        except json.JSONDecodeError:
            continue
    raise ValueError("Ollama returned no usable JSON")


def parse_model_response(raw: str) -> dict[str, list[str]]:
    value = extract_json(raw)
    if isinstance(value, dict) and isinstance(value.get("records"), list):
        value = value["records"]
    elif isinstance(value, dict) and isinstance(value.get("fiches"), list):
        value = value["fiches"]
    if isinstance(value, dict):
        items = [{"page_id": key, "themes": themes} for key, themes in value.items()]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("the JSON root must be an object or an array")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        page_id = str(item.get("page_id") or item.get("id") or "").strip()
        themes = item.get("themes", item.get("theme", []))
        if isinstance(themes, str):
            themes = [themes]
        if not page_id or not isinstance(themes, list):
            continue
        cleaned = []
        for theme in themes:
            normalized = normalize_theme(theme)
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        result[page_id] = cleaned[:2]
    if not result:
        raise ValueError("the model returned no valid records")
    return result


def ollama_ready(model: str) -> bool:
    try:
        response = requests.get(OLLAMA_TAGS, timeout=4)
        response.raise_for_status()
        models = {item.get("name", "") for item in response.json().get("models", [])}
    except (requests.RequestException, ValueError):
        print("Ollama is not available on http://localhost:11434")
        return False
    if model not in models:
        print(f"Local model not found: {model}")
        return False
    return True


def model_prompt(batch: list[dict], known: list[str]) -> str:
    records = [{"page_id": item["page_id"], "text": item["text"]} for item in batch]
    return f"""You classify saved social posts left in a catch-all category.

For every record, assign one or two short themes in the language of the post. Reuse a known theme when it fits. Do not create synonyms for an existing theme. Return an empty list when there is no identifiable subject.

Known themes:
{json.dumps(known, ensure_ascii=False)}

Records:
{json.dumps(records, ensure_ascii=False)}

Return valid JSON only:
{{"records":[{{"page_id":"exact id","themes":["short theme"]}}]}}
Include every page_id. Maximum two themes per record."""


def call_ollama(batch: list[dict], known: list[str], model: str) -> dict[str, list[str]]:
    response = requests.post(
        OLLAMA_GENERATE,
        json={
            "model": model,
            "prompt": model_prompt(batch, known),
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 4000, "num_ctx": 8192},
        },
        timeout=360,
    )
    response.raise_for_status()
    return parse_model_response(response.json().get("response", ""))


def analyze(records: list[dict], cache: dict, refresh: bool, model: str) -> int:
    pending = []
    for record in records:
        cached = cache.get(record["page_id"])
        if (
            not refresh and isinstance(cached, dict)
            and cached.get("hash") == fingerprint(record["text"])
            and isinstance(cached.get("themes"), list)
        ):
            record["themes"] = cached["themes"]
        else:
            pending.append(record)

    failures = 0
    known = Counter(theme for record in records for theme in record["themes"])
    total_batches = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        number = start // BATCH_SIZE + 1
        try:
            result = call_ollama(batch, [theme for theme, _ in known.most_common()], model)
            for record in batch:
                record["themes"] = result.get(record["page_id"], [])
                for theme in record["themes"]:
                    known[theme] += 1
                cache[record["page_id"]] = {
                    "themes": record["themes"],
                    "hash": fingerprint(record["text"]),
                    "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            print(f"Batch {number}/{total_batches}: {len(batch)} records")
        except (requests.RequestException, ValueError, KeyError) as error:
            failures += 1
            print(f"Batch {number}/{total_batches} skipped: {error}")
    return failures


def canonicalize(records: list[dict]) -> None:
    canonical = {}
    for record in records:
        merged = []
        for theme in record["themes"]:
            key = theme_key(theme)
            if not key:
                continue
            canonical.setdefault(key, theme)
            if canonical[key] not in merged:
                merged.append(canonical[key])
        record["themes"] = merged


def render_report(records: list[dict], category: str, minimum: int) -> str:
    grouped = defaultdict(list)
    for record in records:
        for theme in record["themes"]:
            grouped[theme].append(record)
    lines = [
        f"# Themes proposed for {category}", "",
        f"{len(records)} records analyzed locally with Ollama.", "",
        "Nothing was written back to Notion.", "",
    ]
    for theme, items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        lines.extend((f"## {theme}: {len(items)}", ""))
        if len(items) >= minimum:
            lines.extend(("Candidate for a dedicated category.", ""))
        lines.append("Examples:")
        for item in items[:3]:
            title = item["title"].replace("[", "\\[").replace("]", "\\]")[:100]
            lines.append(f"- [{title}]({item['url']})" if item["url"] else f"- {title}")
        lines.append("")
    without = sum(not record["themes"] for record in records)
    lines.extend((f"No identifiable theme: {without}", ""))
    return clean_long_dashes("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min", type=int, default=5, dest="minimum")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.limit < 0 or args.minimum < 1:
        parser.error("--limit must be positive and --min at least 1")

    config = load_json(CONFIG_FILE, {})
    token = config.get("notion_token")
    database_id = config.get("content_ideas_db_id")
    if not token or not database_id:
        raise SystemExit("config.json is missing Notion settings")
    category = (args.category or config.get("catch_all_category") or "INSPIRATION").upper()
    model = config.get("text_model") or "gpt-oss:20b"
    if not ollama_ready(model):
        return 1

    reader = NotionReader(token)
    pages = reader.pages(database_id, category, args.limit)
    if not pages:
        print(f"No [{category}] records found")
        return 0
    records = []
    for number, page in enumerate(pages, 1):
        records.append(page_record(reader, page, category))
        if number % 20 == 0 or number == len(pages):
            print(f"Notion content: {number}/{len(pages)}")

    cache = load_json(CACHE_FILE, {})
    failures = analyze(records, cache, args.refresh, model)
    canonicalize(records)
    temporary = CACHE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(CACHE_FILE)
    REPORT_FILE.write_text(render_report(records, category, args.minimum), encoding="utf-8")
    print(f"Report: {REPORT_FILE.name}. Failed batches: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
