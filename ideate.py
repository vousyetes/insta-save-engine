#!/usr/bin/env python3
"""
Instagram Saves → Content Ideas ideation script.
Fetches all Status=New posts, classifies them into 8 categories,
creates Content Ideas entries, marks originals as Reviewed.

Categories:
  PROMPT    → AI prompts (Claude, ChatGPT, image gen, system prompts)
  REPO      → GitHub repos, open source projects, resources
  OUTIL     → Standalone tools, apps, SaaS, extensions
  WORKFLOW  → Automations, stacks, pipelines, systems
  ASTUCE    → Quick tips, hacks, tricks, short actionable insights
  TUTO      → Step-by-step tutorials, guides, walkthroughs
  VIDÉO IDEA → Content ideas to create, hooks, scripts
  INSPIRATION → Visual/aesthetic/mood references
"""

import json
import time
import re
import requests
from pathlib import Path

from discover_categories import ensure_categories, ollama_up

BASE_DIR = Path(__file__).parent
cfg = json.loads((BASE_DIR / "config.json").read_text())

TOKEN    = cfg["notion_token"]
SAVES_DB = cfg["instagram_saves_db_id"]
IDEAS_DB = cfg["content_ideas_db_id"]
OLLAMA   = "http://localhost:11434/api/generate"
MODEL    = cfg.get("text_model", "gpt-oss:20b")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ── Classification keywords ───────────────────────────────────────────────────

PROMPT_KEYWORDS = [
    "prompt", "prompts", "act as", "you are a", "tu es un", "tu es une",
    "system prompt", "mega prompt", "super prompt", "prompt engineering",
    "copie ce prompt", "utilise ce prompt", "ce prompt", "voici le prompt",
    "copy this prompt", "use this prompt", "here's the prompt",
    "chatgpt prompt", "claude prompt", "gemini prompt", "cursor prompt",
    "imagine prompt", "midjourney prompt", "dalle prompt", "flux prompt",
    "image prompt", "sora prompt",
]

REPO_KEYWORDS = [
    "github.com", "github :", "github:", "open source", "opensource",
    "repository", "repo ", "lien github", "clone this", "fork this",
    "star this", "⭐", "gratuit et open", "disponible sur github",
    "code source", "source code", "npm install", "pip install",
    "hugging face", "huggingface", "arxiv", "research paper",
]

OUTIL_KEYWORDS = [
    "outil", "tool", "app ", "application", "extension", "plugin",
    "logiciel", "software", "service", "plateforme", "platform",
    "télécharge", "download", "try it", "essaie", "disponible sur",
    "lien en bio", "link in bio", "lien bio",
]

OUTIL_EXCLUDE = ["workflow", "automation", "zapier", "make.com", "n8n", "pipeline"]

WORKFLOW_KEYWORDS = [
    "workflow", "automation", "automatisation", "pipeline",
    "make.com", "zapier", "n8n", "airtable",
    "ma stack", "my stack", "mon setup", "my setup",
    "agent ", "multi-agent", "mcp server", "api workflow",
    "système complet", "full system", "end-to-end",
]

ASTUCE_KEYWORDS = [
    "astuce", "tip ", "tips", "trick", "tricks", "hack ", "hacks",
    "bon plan", "conseil", "conseils", "pro tip", "did you know",
    "tu savais que", "savez-vous", "le saviez-vous",
    "raccourci", "shortcut", "quick win", "petite astuce",
    "voici comment", "simple trick", "easy way",
]

TUTO_KEYWORDS = [
    "tuto", "tutoriel", "tutorial", "step by step", "étape par étape",
    "how to", "comment faire", "comment utiliser", "guide complet",
    "learn how", "je vous montre", "i'll show you", "dans ce tuto",
    "in this video i", "dans cette vidéo je", "formation", "cours",
    "masterclass", "walkthrough", "étape 1", "step 1", "partie 1",
]

VIDEO_IDEA_KEYWORDS = [
    "idée vidéo", "video idea", "idée de contenu", "content idea",
    "prochain contenu", "prochain post", "next video",
    "hook", "accroche", "structure vidéo", "format vidéo",
    "script", "créer une vidéo", "créer du contenu",
]

AI_TOOL_AUTHORS = [
    "theaisurfer", "justyn.ai", "emiliencorbineau", "capitalin.ai",
    "dryxio.us", "pirknn", "henriexploria", "ai.explained_",
    "thegptmaster", "aiadvantage", "mreflow", "lxai", "aijaml",
    "tibo_maker", "thegrowthx_", "claudeai", "anthropicai",
]

CATEGORIES = ["PROMPT", "REPO", "OUTIL", "WORKFLOW", "ASTUCE", "TUTO", "VIDÉO IDEA", "INSPIRATION"]

CATEGORY_TO_PILLAR = {
    "PROMPT":      "Tools",
    "REPO":        "Tools",
    "OUTIL":       "Tools",
    "WORKFLOW":    "Process",
    "ASTUCE":      "Teach",
    "TUTO":        "Teach",
    "VIDÉO IDEA":  "Teach",
    "INSPIRATION": "Proof",
}

MEDIA_TO_FORMAT = {
    "Reel":     "Reel",
    "Carousel": "Carousel",
    "Post":     "Carousel",
    "IGTV":     "Long-form Video",
}

# ── Classification logic ──────────────────────────────────────────────────────

def classify(author: str, caption: str, url: str, media_type: str) -> tuple:
    """Returns (category, extracted_idea)"""
    cap_lower = caption.lower()
    auth_lower = author.lower().replace("@", "")
    url_lower = (url or "").lower()

    # PROMPT : highest priority (explicit prompt content)
    for kw in PROMPT_KEYWORDS:
        if kw in cap_lower:
            return ("PROMPT", extract_prompt(caption))

    # REPO : GitHub/open source links or keywords
    repo_score = sum(1 for kw in REPO_KEYWORDS if kw in cap_lower or kw in url_lower)
    if repo_score >= 1:
        return ("REPO", extract_first_lines(caption, 3))

    # WORKFLOW : multi-keyword automation systems
    wf_score = sum(1 for kw in WORKFLOW_KEYWORDS if kw in cap_lower)
    if wf_score >= 2 or (wf_score >= 1 and auth_lower in AI_TOOL_AUTHORS):
        return ("WORKFLOW", extract_first_lines(caption, 3))

    # TUTO : step-by-step content
    for kw in TUTO_KEYWORDS:
        if kw in cap_lower:
            return ("TUTO", extract_first_lines(caption, 4))

    # ASTUCE : quick tips and tricks
    for kw in ASTUCE_KEYWORDS:
        if kw in cap_lower:
            return ("ASTUCE", extract_first_lines(caption, 3))

    # OUTIL : tools/apps (not workflows)
    outil_score = sum(1 for kw in OUTIL_KEYWORDS if kw in cap_lower)
    outil_excluded = any(kw in cap_lower for kw in OUTIL_EXCLUDE)
    if outil_score >= 2 and not outil_excluded:
        return ("OUTIL", extract_first_lines(caption, 3))

    # VIDÉO IDEA : content creation hooks/scripts
    for kw in VIDEO_IDEA_KEYWORDS:
        if kw in cap_lower:
            return ("VIDÉO IDEA", extract_first_lines(caption, 3))

    # AI tool authors posting reels = likely useful content
    if auth_lower in AI_TOOL_AUTHORS and media_type in ("Reel", "Carousel"):
        return ("VIDÉO IDEA", extract_first_lines(caption, 3))

    # INSPIRATION : everything else
    return ("INSPIRATION", "")


def extract_prompt(caption: str) -> str:
    lines = caption.split("\n")
    prompt_lines = []
    capturing = False
    for line in lines:
        if any(kw in line.lower() for kw in PROMPT_KEYWORDS):
            capturing = True
        if capturing:
            prompt_lines.append(line)
        if len(prompt_lines) > 15:
            break
    return "\n".join(prompt_lines).strip()[:800] if prompt_lines else caption[:500]


def extract_first_lines(caption: str, n: int) -> str:
    lines = [l.strip() for l in caption.split("\n") if l.strip() and not l.strip().startswith("#")]
    return "\n".join(lines[:n])

# ── AI classification (adapts to the user's own categories) ───────────────────

def pillar_for(category: str, user_cats: list) -> str:
    """Pillar of a category, from the user's generated scheme (fallback Teach)."""
    for c in user_cats:
        if c["name"].upper() == category.upper():
            return c.get("pillar", "Teach")
    return CATEGORY_TO_PILLAR.get(category, "Teach")


def classify_llm(caption: str, url: str, media_type: str, user_cats: list) -> tuple:
    """Ask the local model to file a post into ONE of the user's categories.
    Returns (category_name, idea_text). Returns (None, "") so the caller can
    fall back to the rule-based classifier when the model can't decide."""
    text = (caption or url or "").strip()
    if not text:
        return (None, "")
    names = [c["name"] for c in user_cats]
    cat_block = "\n".join(f"- {c['name']}: {c.get('description','')}" for c in user_cats)
    prompt = (
        "You file a saved Instagram post into exactly one category.\n\n"
        f"Categories:\n{cat_block}\n\n"
        f"Post:\n{text[:1500]}\n\n"
        "Answer with ONLY the exact category name from the list, nothing else."
    )
    try:
        r = requests.post(OLLAMA, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 40},
        }, timeout=120)
        r.raise_for_status()
        pick = r.json().get("response", "").strip().upper()
    except Exception:
        return (None, "")
    # match the answer to a real category name (exact, then contained)
    match = next((n for n in names if n.upper() == pick), None)
    if not match:
        match = next((n for n in names if n.upper() in pick or pick in n.upper()), None)
    if not match:
        return (None, "")
    return (match, extract_first_lines(caption, 3))

# ── Notion helpers ────────────────────────────────────────────────────────────

def fetch_new_posts() -> list:
    posts = []
    cursor = None
    while True:
        body = {
            "filter": {"property": "Status", "select": {"equals": "New"}},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{SAVES_DB}/query",
            headers=HEADERS, json=body, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        posts.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.3)
    return posts


def get_prop_text(props, key):
    p = props.get(key, {})
    if "rich_text" in p:
        items = p["rich_text"]
        return items[0]["text"]["content"] if items else ""
    if "title" in p:
        items = p["title"]
        return items[0]["text"]["content"] if items else ""
    if "url" in p:
        return p["url"] or ""
    if "select" in p:
        s = p["select"]
        return s["name"] if s else ""
    return ""


def get_thumbnail_from_ig(url: str) -> str:
    if not url:
        return ""
    try:
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        }, timeout=8)
        m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def create_idea(author, caption, url, category, idea_text, media_type, thumbnail_url="", pillar=None):
    if pillar is None:
        pillar = CATEGORY_TO_PILLAR.get(category, "Teach")
    fmt = MEDIA_TO_FORMAT.get(media_type, "Carousel")

    if idea_text and len(idea_text) > 5:
        title_base = idea_text.split("\n")[0][:80]
    elif caption:
        title_base = caption.split("\n")[0][:80]
    else:
        title_base = f"@{author}"
    title = f"[{category}] {title_base}"

    angle = idea_text[:500] if idea_text else extract_first_lines(caption, 2)
    hook = caption.split("\n")[0][:300] if caption else ""

    body_parts = []
    if url:
        body_parts.append(f"🔗 **Source :** {url}")
    body_parts.append(f"👤 **Auteur :** @{author}")
    if idea_text and len(idea_text) > 10:
        body_parts.append(f"\n**Contenu extrait :**\n\n{idea_text[:1500]}")
    elif caption:
        body_parts.append(f"\n**Caption originale :**\n\n{caption[:1000]}")
    content = "\n\n".join(body_parts)

    props = {
        "Name":         {"title": [{"text": {"content": title[:200]}}]},
        "Status":       {"select": {"name": "Not started"}},
        "Pillar":       {"select": {"name": pillar}},
        "Format":       {"select": {"name": fmt}},
        "Platform":     {"multi_select": [{"name": "Instagram"}]},
        "Angle":        {"rich_text": [{"text": {"content": angle[:2000]}}]},
        "Hook Options": {"rich_text": [{"text": {"content": hook[:2000]}}]},
    }

    payload = {
        "parent": {"database_id": IDEAS_DB},
        "properties": props,
        "children": [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]}
        }] if content else []
    }
    if thumbnail_url:
        payload["cover"] = {"type": "external", "external": {"url": thumbnail_url}}

    r = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload, timeout=15)
    if r.status_code not in (200, 201):
        print(f"  ⚠ Notion error {r.status_code}: {r.text[:200]}")
        return False
    return True


def mark_reviewed(page_id):
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"properties": {"Status": {"select": {"name": "Reviewed"}}}},
        timeout=15
    )
    return r.status_code in (200, 201)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # First pass: derive a category scheme that fits THIS person's saves, using
    # the local model. On later runs it's already in config.json, so this is a
    # no-op. With no Ollama (light mode) it returns [] and we fall back to the
    # built-in keyword scheme below.
    user_cats = ensure_categories()
    use_llm = bool(user_cats) and ollama_up()
    if use_llm:
        print(f"Classifying with your {len(user_cats)} categories (local AI).\n")
    else:
        print("Classifying with the built-in keyword scheme "
              "(light mode / Ollama off).\n")

    print("Fetching all Status=New posts from Notion...")
    posts = fetch_new_posts()
    print(f"Found {len(posts)} posts to process.\n")

    counts = {}
    errors = 0

    for i, page in enumerate(posts, 1):
        props = page.get("properties", {})
        page_id = page["id"]
        author = get_prop_text(props, "Author")
        caption = get_prop_text(props, "Caption")
        url = get_prop_text(props, "URL") or props.get("URL", {}).get("url", "")
        media_type = get_prop_text(props, "Type")

        # AI classification into the user's own categories; fall back to the
        # rule-based classifier per-post if the model can't decide.
        category, idea_text = (None, "")
        if use_llm:
            category, idea_text = classify_llm(caption, url, media_type, user_cats)
        if not category:
            category, idea_text = classify(author, caption, url, media_type)
        pillar = pillar_for(category, user_cats)
        counts[category] = counts.get(category, 0) + 1

        thumbnail_url = ""
        page_cover = page.get("cover")
        if page_cover and page_cover.get("type") == "external":
            thumbnail_url = page_cover["external"].get("url", "")
        if not thumbnail_url and url:
            thumbnail_url = get_thumbnail_from_ig(url)

        ok = create_idea(author, caption, url, category, idea_text, media_type,
                         thumbnail_url, pillar=pillar)
        if ok:
            mark_reviewed(page_id)
            print(f"[{i:3}/{len(posts)}] ★ [{category:<14}] @{author} : {(caption or url)[:50]}")
        else:
            errors += 1
            print(f"[{i:3}/{len(posts)}] ✗ Failed for @{author}")

        time.sleep(0.35)

    print(f"\n{'─'*60}")
    print(f"Done! {len(posts) - errors} processed, {errors} errors.")
    print(f"\nBreakdown:")
    for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        bar = "█" * (n // 5) if n else ""
        print(f"  {cat:<16} {n:>4}  {bar}")


if __name__ == "__main__":
    main()
