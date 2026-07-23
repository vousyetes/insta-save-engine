#!/usr/bin/env python3
"""
setup_notion.py — Create the two Notion databases automatically.

Instead of building the databases by hand, this script asks Notion to create
them for you, with the exact property schemas the pipeline expects, then writes
the two database IDs straight into config.json.

What you need first (see the README, "Notion setup"):
  1. A Notion integration token  → paste it in config.json as "notion_token".
  2. A normal Notion page, SHARED with that integration (Notion calls it a
     "connection"). This page will hold the two databases.
     → paste its ID/URL in config.json as "notion_parent_page_id",
       OR pass it as an argument:  python setup_notion.py <page-url-or-id>

Run:
    .venv/bin/python setup_notion.py
    # or
    .venv/bin/python setup_notion.py https://www.notion.so/Your-Page-<id>

It is safe to re-run: it always creates fresh databases and updates config.json
with the new IDs.
"""

import json
import re
import sys
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
NOTION_API  = "https://api.notion.com/v1"
NOTION_VER  = "2022-06-28"


# ── Schemas ───────────────────────────────────────────────────────────────────
# These match exactly what sync.py / ideate.py / extract.py write and read.

INSTAGRAM_SAVES_PROPERTIES = {
    "Name":       {"title": {}},
    "URL":        {"url": {}},
    "Type":       {"select": {"options": [
        {"name": "Post"}, {"name": "Reel"},
        {"name": "Carousel"}, {"name": "IGTV"},
    ]}},
    "Author":     {"rich_text": {}},
    "Status":     {"select": {"options": [
        {"name": "New"}, {"name": "Reviewed"},
    ]}},
    "Media ID":   {"rich_text": {}},
    "Saved":      {"date": {}},
    "Caption":    {"rich_text": {}},
    "Collection": {"select": {}},  # options fill in on their own (collection names)
}

CONTENT_IDEAS_PROPERTIES = {
    "Name":         {"title": {}},
    "Week Of":      {"date": {}},
    "Status":       {"select": {"options": [
        {"name": "Not started"}, {"name": "In progress"}, {"name": "Done"},
    ]}},
    "Format":       {"select": {"options": [
        {"name": "Carousel"}, {"name": "Reel"},
        {"name": "Short Video"}, {"name": "Long-form Video"},
    ]}},
    "Platform":     {"multi_select": {"options": [
        {"name": "Instagram"}, {"name": "TikTok"}, {"name": "YouTube"},
    ]}},
    "Priority":     {"select": {"options": [
        {"name": "High"}, {"name": "Medium"}, {"name": "Low"},
    ]}},
    "Pillar":       {"select": {"options": [
        {"name": "Teach"}, {"name": "Proof"},
        {"name": "Tools"}, {"name": "Process"},
    ]}},
    "Angle":        {"rich_text": {}},
    "Hook Options": {"rich_text": {}},
}


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def extract_page_id(raw: str) -> str:
    """Accept a Notion page URL or a raw ID, return a 32-char hex id.

    Careful: Notion page slugs look like `Some-Title-<32hex>`, and title words
    can end in hex letters (a-f). We must NOT strip the dashes before matching,
    or a title ending in e.g. "Engine" would shift the captured id by one char.
    """
    raw = (raw or "").strip().split("?")[0]
    # 1) Dashed UUID form (…/1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d)
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw)
    if m:
        return m.group(0).replace("-", "")
    # 2) Undashed: the id is the trailing 32 hex chars of the last path segment.
    #    The dash before it (from the slug) breaks the hex run, so $ anchors it right.
    seg = raw.rstrip("/").split("/")[-1]
    m = re.search(r"([0-9a-fA-F]{32})$", seg)
    if m:
        return m.group(1)
    # 3) Fallback: any 32-hex run in the segment.
    ids = re.findall(r"[0-9a-fA-F]{32}", seg)
    return ids[-1] if ids else ""


def create_database(token: str, parent_page_id: str, title: str,
                    properties: dict, icon: str) -> str:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": icon},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": properties,
    }
    r = requests.post(f"{NOTION_API}/databases", headers=headers(token),
                      json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Notion refused to create '{title}' "
                           f"({r.status_code}): {r.text[:400]}")
    return r.json()["id"]


def main() -> int:
    if not CONFIG_FILE.exists():
        print("✗ config.json not found. Copy config.example.json → config.json first.")
        return 1
    cfg = json.loads(CONFIG_FILE.read_text())

    token = (cfg.get("notion_token") or "").strip()
    if not token or token.startswith("ntn_PASTE"):
        print("✗ No Notion token. Paste your integration token into config.json "
              "as \"notion_token\" first.")
        return 1

    raw_parent = sys.argv[1] if len(sys.argv) > 1 else cfg.get("notion_parent_page_id", "")
    parent_page_id = extract_page_id(raw_parent)
    if not parent_page_id:
        print("✗ No parent page. Create a Notion page, share it with your "
              "integration, then either:")
        print("   • paste its URL/ID into config.json as \"notion_parent_page_id\", or")
        print("   • run:  .venv/bin/python setup_notion.py <page-url-or-id>")
        return 1

    print("── Insta Save Engine — Notion setup ────────────────────────────────")
    print(f"Parent page: {parent_page_id}\n")

    try:
        print("Creating «Instagram Saves» …")
        saves_id = create_database(token, parent_page_id, "Instagram Saves",
                                   INSTAGRAM_SAVES_PROPERTIES, "📥")
        print(f"  ✓ {saves_id}")

        print("Creating «Content Ideas» …")
        ideas_id = create_database(token, parent_page_id, "Content Ideas",
                                   CONTENT_IDEAS_PROPERTIES, "💡")
        print(f"  ✓ {ideas_id}")
    except RuntimeError as e:
        print(f"\n✗ {e}\n")
        print("Most common cause: the page is not shared with the integration.")
        print("Open the page → ••• menu → Connections → add your integration,")
        print("then run this script again.")
        return 1

    cfg["instagram_saves_db_id"] = saves_id
    cfg["content_ideas_db_id"]   = ideas_id
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    print("\n✓ Done. Both databases created and their IDs written to config.json.")
    print("  You can now run the pipeline:  .venv/bin/python sync.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
