#!/usr/bin/env python3
"""
discover_categories.py : build a category scheme that fits YOUR saves.

The old pipeline shipped a fixed set of categories tuned for AI content
(PROMPT, REPO, OUTIL...). That only makes sense if you save AI posts. This
script instead reads a sample of your own saved captions and asks the local
model (gpt-oss via Ollama) to propose 6-10 categories that actually match what
you save, each with a short name, a one-line description, and a content pillar.

The result is written to config.json under "categories". You can edit it by hand
afterwards. ideate.py then classifies every post into these categories.

Runs on the first pass automatically (ideate.py calls it when no categories are
set yet and Ollama is up). You can also run it on its own:

    .venv/bin/python discover_categories.py            # generate if not set yet
    .venv/bin/python discover_categories.py --force     # regenerate from scratch
    .venv/bin/python discover_categories.py --show       # print current categories

Needs Ollama (full mode). In light mode there is no model to derive categories,
so ideate.py falls back to a built-in generic keyword scheme.
"""

import json
import sys
import time
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
cfg = json.loads(CONFIG_FILE.read_text())

TOKEN    = cfg["notion_token"]
SAVES_DB = cfg["instagram_saves_db_id"]
OLLAMA   = "http://localhost:11434/api/generate"
MODEL    = cfg.get("text_model", "gpt-oss:20b")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

VALID_PILLARS = {"Teach", "Proof", "Tools", "Process"}
SAMPLE_SIZE   = 100


def ollama_up() -> bool:
    try:
        requests.get("http://localhost:11434/api/tags", timeout=3)
        return True
    except Exception:
        return False


def sample_captions(n: int = SAMPLE_SIZE) -> list:
    """Pull up to n non-empty captions from the Instagram Saves database."""
    caps, cursor = [], None
    while len(caps) < n:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{SAVES_DB}/query",
            headers=HEADERS, json=body, timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        for p in data.get("results", []):
            props = p.get("properties", {})
            rt = props.get("Caption", {}).get("rich_text", [])
            text = rt[0]["text"]["content"] if rt else ""
            author = ""
            art = props.get("Author", {}).get("rich_text", [])
            if art:
                author = art[0]["text"]["content"]
            text = (text or "").strip()
            if len(text) > 15:
                caps.append(f"@{author}: {text[:280]}")
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.2)
    return caps[:n]


DISCOVERY_PROMPT = """You are organizing someone's saved Instagram posts. Below is a sample of the captions they saved. Your job is to design a category scheme that fits THIS person's actual interests, so every future save can be filed into one bucket.

Rules:
- Propose between 6 and 10 categories. Not fewer, not more.
- Each category: a SHORT name in UPPERCASE (one or two words), a one-line description of what goes in it, and a content pillar chosen from exactly: Teach, Proof, Tools, Process.
- Make them fit the sample. If it's cooking, use cooking categories. If it's AI, use AI categories. Do not force a generic scheme.
- Cover the whole sample: include a catch-all like INSPIRATION or REFERENCE for the visual/mood posts that don't carry actionable info.
- Write names and descriptions in the SAME language as the captions.

Return ONLY valid JSON, nothing else, in exactly this shape:
{"categories":[{"name":"NAME","description":"one line","pillar":"Teach"}, ...]}

SAMPLE OF SAVED CAPTIONS:
{sample}

JSON:"""


def _parse_json(text: str) -> dict:
    """Best-effort extraction of the JSON object from the model output."""
    text = text.strip()
    # strip code fences if any
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def generate_categories(captions: list) -> list:
    """Ask the local model for a category scheme. Returns a cleaned list."""
    sample = "\n".join(f"- {c}" for c in captions)
    prompt = DISCOVERY_PROMPT.replace("{sample}", sample[:8000])

    for attempt in range(3):
        try:
            r = requests.post(OLLAMA, json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                # gpt-oss reasons internally; give it room or the output comes back empty.
                "options": {"temperature": 0.3, "num_predict": 2000},
            }, timeout=240)
            r.raise_for_status()
            raw = r.json().get("response", "").strip()
            if not raw:
                time.sleep(1)
                continue
            data = _parse_json(raw)
            cats = []
            seen = set()
            for c in data.get("categories", []):
                name = str(c.get("name", "")).strip().upper()
                desc = str(c.get("description", "")).strip()
                pillar = str(c.get("pillar", "")).strip().title()
                if pillar not in VALID_PILLARS:
                    pillar = "Teach"
                if name and name not in seen:
                    seen.add(name)
                    cats.append({"name": name, "description": desc, "pillar": pillar})
            if len(cats) >= 4:
                return cats[:10]
        except requests.exceptions.ConnectionError:
            print("  ⚠ Ollama unreachable : run 'ollama serve' first.")
            return []
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            print(f"  ⚠ Could not generate categories: {e}")
    return []


def save_categories(cats: list) -> None:
    data = json.loads(CONFIG_FILE.read_text())
    data["categories"] = cats
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def ensure_categories(force: bool = False) -> list:
    """Return the configured categories, generating them once if needed.
    Called by ideate.py on the first pass. Returns [] if it can't (no Ollama)."""
    existing = cfg.get("categories", [])
    if existing and not force:
        return existing
    if not ollama_up():
        return existing  # keep whatever is there ([] → ideate falls back to rules)
    print("Discovering categories from your saves (first pass)…")
    caps = sample_captions()
    if len(caps) < 8:
        print(f"  Only {len(caps)} captions found : run sync.py first. Skipping.")
        return existing
    cats = generate_categories(caps)
    if cats:
        save_categories(cats)
        print(f"  ✓ {len(cats)} categories generated and saved to config.json:")
        for c in cats:
            print(f"     [{c['name']}] ({c['pillar']}) : {c['description'][:60]}")
    return cats or existing


def main():
    args = sys.argv[1:]
    if "--show" in args:
        cats = cfg.get("categories", [])
        if not cats:
            print("No categories set yet. Run sync.py then ideate.py (or this script).")
            return
        for c in cats:
            print(f"[{c['name']}] ({c.get('pillar','?')}) : {c.get('description','')}")
        return
    force = "--force" in args
    if not ollama_up():
        print("Ollama is not running. Start it (ollama serve) : category discovery "
              "needs the local model. In light mode, ideate.py uses a generic scheme instead.")
        return
    ensure_categories(force=force)


if __name__ == "__main__":
    main()
