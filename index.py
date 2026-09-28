#!/usr/bin/env python3
"""Build a compact local and offline index of saved posts."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "index"
JSONL_FILE = OUTPUT_DIR / "index.jsonl"
MARKDOWN_FILE = OUTPUT_DIR / "index.md"
HTML_FILE = OUTPUT_DIR / "vault.html"
THEMES_FILE = BASE_DIR / "themes-cache.json"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
URL_RE = re.compile(r"https?://[^\s<>\])]+")
PREFIX_RE = re.compile(r"^\[([^\]]+)\]\s*")
GENERIC_RE = re.compile(
    r"^(this post|the post|interesting content|key takeaway|general inspiration|"
    r"ce post|cette publication|contenu interessant|a retenir|inspiration generale)\b",
    re.IGNORECASE,
)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def without_accents(text: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(character)
    ).lower()


def clean_text(text: str) -> str:
    lines = []
    for line in (text or "").replace("\u2014", " - ").replace("\u2013", " - ").splitlines():
        lines.append(re.sub(r"[ \t]+", " ", line).strip())
    return "\n".join(lines).strip()


def rich_text(property_value: dict) -> str:
    items = property_value.get("title") or property_value.get("rich_text") or []
    return "".join(item.get("plain_text", "") for item in items).strip()


def block_text(block: dict) -> str:
    return rich_text(block.get(block.get("type", ""), {}))


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip().rstrip(".,;:!?"))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def first_url(text: str) -> str:
    match = URL_RE.search(text or "")
    return canonical_url(match.group(0)) if match else ""


def author_from_body(text: str) -> str:
    match = re.search(r"Author\s*:\*{0,2}\s*@?([\w.]+)", text or "", re.IGNORECASE)
    if not match:
        match = re.search(r"Auteur\s*:\*{0,2}\s*@?([\w.]+)", text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def category_and_title(name: str) -> tuple[str, str]:
    match = PREFIX_RE.match(name or "")
    if not match:
        return "UNCATEGORIZED", (name or "Untitled").strip()
    return match.group(1).upper(), PREFIX_RE.sub("", name).strip() or "Untitled"


def reduce_summary(angle: str, maximum: int = 520) -> str:
    candidates = []
    text = re.sub(r"^[\s🧠👁]+", "", (angle or "").replace("\r", "\n"))
    for line in re.sub(r"\n{2,}", "\n", text).splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line).strip(" *\t")
        if not line or GENERIC_RE.search(without_accents(line)):
            continue
        pieces = re.split(r"(?<=[.!?])\s+", line) if len(line) > 220 else [line]
        for piece in pieces:
            piece = re.sub(r"\s+", " ", piece).strip()
            if len(piece) >= 8 and not GENERIC_RE.search(without_accents(piece)):
                candidates.append(piece)
    if not candidates:
        return ""

    def score(line: str) -> int:
        return (
            (4 if URL_RE.search(line) else 0)
            + (3 if re.search(r"\d", line) else 0)
            + (2 if re.search(r"\b(?:github|notion|n8n|ollama|whisper|api)\b", line, re.I) else 0)
            + (1 if ":" in line else 0)
        )

    chosen = sorted(enumerate(candidates), key=lambda item: (-score(item[1]), item[0]))[:4]
    output = []
    size = 0
    for _, line in sorted(chosen):
        room = maximum - size - (1 if output else 0)
        if room < 24:
            break
        if len(line) > room:
            line = line[:max(20, room - 3)].rstrip() + "..."
        output.append(line)
        size += len(line) + (1 if len(output) > 1 else 0)
    return "\n".join(output)


class NotionReader:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self) -> None:
        with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + 0.34

    def request(self, method: str, path: str, **kwargs) -> dict:
        last = None
        for attempt in range(6):
            self.wait()
            try:
                response = requests.request(
                    method, f"{NOTION_API}/{path.lstrip('/')}",
                    headers=self.headers, timeout=35, **kwargs,
                )
            except requests.RequestException:
                if attempt == 5:
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

    def database(self, database_id: str) -> list[dict]:
        pages = []
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"databases/{database_id}/query", json=body)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

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


def source_record(page: dict) -> dict:
    properties = page.get("properties", {})
    saved = (properties.get("Saved", {}).get("date") or {}).get("start")
    return {
        "url": canonical_url(properties.get("URL", {}).get("url") or ""),
        "author": rich_text(properties.get("Author", {})).lstrip("@"),
        "date_saved": (saved or page.get("created_time", "")[:10])[:10],
    }


def read_bodies(reader: NotionReader, pages: list[dict]) -> dict[str, str]:
    total = len(pages)
    completed = 0
    lock = threading.Lock()

    def read(page: dict) -> tuple[str, str]:
        nonlocal completed
        text = "\n".join(filter(None, (block_text(block) for block in reader.blocks(page["id"]))))
        with lock:
            completed += 1
            if completed % 100 == 0 or completed == total:
                print(f"Notion bodies: {completed}/{total}", flush=True)
        return page["id"], text

    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(read, pages))


def idea_record(page: dict, body: str, sources: dict, themes: dict) -> dict:
    properties = page.get("properties", {})
    category, title = category_and_title(rich_text(properties.get("Name", {})))
    url = first_url(body)
    source = sources.get(url, {})
    return {
        "id": page["id"],
        "category": clean_text(category),
        "title": clean_text(title),
        "author": clean_text(source.get("author") or author_from_body(body)),
        "url": url,
        "date_saved": source.get("date_saved") or page.get("created_time", "")[:10],
        "themes": [clean_text(theme) for theme in (themes.get(page["id"], {}).get("themes") or [])[:2]],
        "summary": clean_text(reduce_summary(rich_text(properties.get("Angle", {})))),
        "notion_url": page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}",
    }


def markdown(records: list[dict]) -> str:
    groups = defaultdict(list)
    for record in records:
        groups[record["category"]].append(record)
    lines = [
        "# Saved posts index", "",
        f"{len(records)} records. Updated {datetime.now().date().isoformat()}.", "",
    ]
    for category in sorted(groups, key=without_accents):
        lines.extend((f"## {category}", ""))
        for record in sorted(groups[category], key=lambda item: item["date_saved"], reverse=True):
            link = record["url"] or record["notion_url"]
            author = f"@{record['author']}" if record["author"] else "unknown author"
            themes = ", ".join(record["themes"]) or "no theme"
            summary = record["summary"].replace("\n", " / ") or "no summary"
            lines.extend((f"### [{record['title']}]({link})", f"{author} | {themes} | {summary}", ""))
    return "\n".join(lines).rstrip() + "\n"


HTML_TEMPLATE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Saved posts</title><style>
:root{font-family:system-ui,sans-serif;color:#18222a;background:#f3f6f8}*{box-sizing:border-box}body{margin:0}header{position:sticky;top:0;padding:18px;background:#f3f6f8;border-bottom:1px solid #ccd5db}header div,main{max-width:900px;margin:auto}h1{margin:0 0 12px}input,select{font:inherit;padding:10px;border:1px solid #7c8b95;border-radius:7px;background:white}input{width:min(100%,600px)}main{padding:18px}.controls{display:flex;gap:8px;flex-wrap:wrap}.grid{display:grid;gap:12px}.card{padding:16px;background:white;border:1px solid #ccd5db;border-radius:8px}.meta{color:#53636e;font-size:.85rem}.themes{display:flex;gap:6px;flex-wrap:wrap}.theme{padding:2px 7px;background:#e5eef3;border-radius:4px;font-size:.8rem}a{color:#075985}@media(min-width:760px){.grid{grid-template-columns:1fr 1fr}}
</style></head><body><header><div><h1>Saved posts</h1><div class="controls"><input id="q" type="search" placeholder="Search titles, themes, authors and summaries"><select id="category"><option value="">All categories</option></select></div><p id="count"></p></div></header><main><div class="grid" id="grid"></div></main>
<script>const DATA=__DATA__;const q=document.querySelector('#q'),category=document.querySelector('#category'),grid=document.querySelector('#grid'),count=document.querySelector('#count');const esc=s=>{const d=document.createElement('div');d.textContent=s||'';return d.innerHTML};const norm=s=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();[...new Set(DATA.map(x=>x.category))].sort().forEach(v=>category.insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));function render(){const words=norm(q.value).split(/\s+/).filter(Boolean);const rows=DATA.filter(x=>(!category.value||x.category===category.value)&&words.every(w=>norm([x.title,x.author,x.summary,...x.themes].join(' ')).includes(w)));count.textContent=`${rows.length} / ${DATA.length}`;grid.innerHTML=rows.map(x=>`<article class="card"><div class="meta">${esc(x.category)} | ${esc(x.author?'@'+x.author:'unknown author')} | ${esc(x.date_saved)}</div><h2>${esc(x.title)}</h2><p>${esc(x.summary||'No summary')}</p><div class="themes">${x.themes.map(t=>`<span class="theme">${esc(t)}</span>`).join('')}</div><p><a href="${esc(x.url||x.notion_url)}">Open source</a> | <a href="${esc(x.notion_url)}">Notion</a></p></article>`).join('')||'<p>No matching record.</p>'}q.oninput=render;category.onchange=render;render();</script></body></html>'''


def write_outputs(records: list[dict]) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    JSONL_FILE.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    MARKDOWN_FILE.write_text(markdown(records), encoding="utf-8")
    data = json.dumps(records, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    HTML_FILE.write_text(HTML_TEMPLATE.replace("__DATA__", data), encoding="utf-8")


def copy_outputs(directory: str) -> str:
    if not directory:
        return "disabled"
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MARKDOWN_FILE, target / MARKDOWN_FILE.name)
    shutil.copy2(HTML_FILE, target / HTML_FILE.name)
    return str(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-copy", action="store_true", help="ignore offline_index_copy_dir")
    args = parser.parse_args()
    config = load_json(BASE_DIR / "config.json", {})
    required = ("notion_token", "content_ideas_db_id", "instagram_saves_db_id")
    if any(not config.get(key) for key in required):
        raise SystemExit("config.json is missing Notion settings")

    reader = NotionReader(config["notion_token"])
    print("Reading Notion databases...", flush=True)
    source_pages = reader.database(config["instagram_saves_db_id"])
    sources = {
        record["url"]: record
        for record in (source_record(page) for page in source_pages)
        if record["url"]
    }
    idea_pages = reader.database(config["content_ideas_db_id"])
    bodies = read_bodies(reader, idea_pages)
    themes = load_json(THEMES_FILE, {})
    records = [
        idea_record(page, bodies.get(page["id"], ""), sources, themes)
        for page in idea_pages
    ]
    records.sort(key=lambda item: (item["category"], item["date_saved"], item["title"]))
    write_outputs(records)
    copied = "disabled" if args.no_copy else copy_outputs(config.get("offline_index_copy_dir", ""))
    print(f"Indexed: {len(records)} records")
    for category, count in sorted(Counter(item["category"] for item in records).items()):
        print(f"  {category}: {count}")
    print(f"HTML: {HTML_FILE}")
    print(f"Optional copy: {copied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
