#!/usr/bin/env python3
"""
Query Content Ideas from Notion by category or keyword.
Usage:
  python query.py                    # summary of all categories
  python query.py REPO               # all repos
  python query.py PROMPT claude      # prompts containing "claude"
  python query.py --keyword n8n      # any category, keyword search
  python query.py --limit 20 ASTUCE  # limit results
"""

import json
import sys
import re
import requests
from pathlib import Path

BASE_DIR = Path(__file__).parent
cfg = json.loads((BASE_DIR / "config.json").read_text())

TOKEN    = cfg["notion_token"]
IDEAS_DB = cfg["content_ideas_db_id"]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CATEGORIES = ["PROMPT", "REPO", "OUTIL", "WORKFLOW", "ASTUCE", "TUTO", "VIDÉO IDEA", "INSPIRATION"]

PILLAR_MAP = {
    "PROMPT": "Tools", "REPO": "Tools", "OUTIL": "Tools",
    "WORKFLOW": "Process",
    "ASTUCE": "Teach", "TUTO": "Teach", "VIDÉO IDEA": "Teach",
    "INSPIRATION": "Proof",
}


def fetch_all_ideas(category: str = None, keyword: str = None, limit: int = 50) -> list:
    pages = []
    cursor = None

    # Build filter
    filters = []
    if category:
        # Match category from Name field prefix [CATEGORY]
        filters.append({
            "property": "Name",
            "title": {"contains": f"[{category}]"}
        })
    if keyword:
        filters.append({
            "property": "Name",
            "title": {"contains": keyword}
        })

    while True:
        body = {"page_size": 100}
        if filters:
            body["filter"] = {"and": filters} if len(filters) > 1 else filters[0]
        if cursor:
            body["start_cursor"] = cursor

        r = requests.post(
            f"https://api.notion.com/v1/databases/{IDEAS_DB}/query",
            headers=HEADERS, json=body, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more") or len(pages) >= limit:
            break
        cursor = data.get("next_cursor")

    return pages[:limit]


def get_name(page) -> str:
    titles = page.get("properties", {}).get("Name", {}).get("title", [])
    return titles[0]["text"]["content"] if titles else "(sans titre)"


def get_angle(page) -> str:
    rt = page.get("properties", {}).get("Angle", {}).get("rich_text", [])
    return rt[0]["text"]["content"][:200] if rt else ""


def get_url(page) -> str:
    return f"https://www.notion.so/{page['id'].replace('-', '')}"


def format_page(page, i: int) -> str:
    name = get_name(page)
    angle = get_angle(page)
    notion_url = get_url(page)
    lines = [f"{i}. **{name}**"]
    if angle:
        lines.append(f"   {angle[:120]}{'...' if len(angle) > 120 else ''}")
    lines.append(f"   → {notion_url}")
    return "\n".join(lines)


def summary_mode():
    print("📊 Content Ideas : Résumé par catégorie\n")
    total = 0
    for cat in CATEGORIES:
        pages = fetch_all_ideas(category=cat, limit=500)
        n = len(pages)
        total += n
        bar = "█" * (n // 10) if n else "·"
        print(f"  {cat:<15} {n:>4}  {bar}")
    print(f"\n  {'TOTAL':<15} {total:>4}")


def is_teaser(page) -> bool:
    """Le contenu réel n'est pas dans le post (flag ⏳ posé par extract.py)."""
    return "⏳" in get_angle(page)


def search_mode(category: str, keyword: str, limit: int, show_teasers: bool = False):
    label = f"[{category}]" if category else "toutes catégories"
    kw_label = f" + '{keyword}'" if keyword else ""
    print(f"🔍 {label}{kw_label} : {limit} max\n")

    # On récupère large puis on filtre les teasers (sauf --teasers)
    raw = fetch_all_ideas(category=category, keyword=keyword, limit=limit * 4)
    if not raw and category and keyword:
        raw = fetch_all_ideas(keyword=keyword, limit=limit * 4)
        if raw:
            print(f"  (catégorie non trouvée, résultats pour '{keyword}' toutes catégories)\n")

    hidden = 0
    if not show_teasers:
        before = len(raw)
        raw = [p for p in raw if not is_teaser(p)]
        hidden = before - len(raw)

    pages = raw[:limit]
    if not pages:
        msg = "  Aucun résultat exploitable."
        if hidden:
            msg += f" ({hidden} teaser(s) ⏳ masqué(s) : ajoute --teasers pour les voir)"
        print(msg)
        return

    note = f" ({hidden} teaser(s) ⏳ masqué(s))" if hidden else ""
    print(f"  {len(pages)} résultat(s) exploitable(s){note} :\n")
    for i, page in enumerate(pages, 1):
        print(format_page(page, i))
        print()


def main():
    args = sys.argv[1:]
    limit = 20
    keyword = None
    category = None

    # --teasers : inclure aussi les posts dont le contenu est en DM (flag ⏳)
    show_teasers = "--teasers" in args
    if show_teasers:
        args.remove("--teasers")

    # Parse --limit
    if "--limit" in args:
        idx = args.index("--limit")
        limit = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    # Parse --keyword
    if "--keyword" in args:
        idx = args.index("--keyword")
        keyword = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    # Remaining args: first is category, rest are extra keywords
    if args:
        first = args[0].upper()
        if first in CATEGORIES or first in [c.upper() for c in CATEGORIES]:
            # Find exact match (case-insensitive)
            for cat in CATEGORIES:
                if cat.upper() == first:
                    category = cat
                    break
            args = args[1:]
        # Any remaining arg = keyword
        if args and not keyword:
            keyword = " ".join(args)

    if not category and not keyword:
        summary_mode()
    else:
        search_mode(category, keyword, limit, show_teasers)


if __name__ == "__main__":
    main()
