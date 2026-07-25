#!/usr/bin/env python3
"""
AI extraction pass — enrichit les Content Ideas Notion avec Ollama (local, gratuit).

Pour chaque post classifié, extrait l'essentiel selon la catégorie :
  PROMPT     → le prompt utilisable, nettoyé, copy-paste ready
  REPO       → nom du repo + URL GitHub + ce qu'il fait en 1 phrase
  OUTIL      → nom de l'outil + ce qu'il fait + lien
  WORKFLOW   → les étapes numérotées du workflow
  ASTUCE     → le conseil en 1-2 phrases actionnables
  TUTO       → les étapes clés numérotées
  VIDÉO IDEA → hook + structure de contenu
  INSPIRATION → mots-clés mood + style visuel

Usage:
  python extract.py              # process all non-extracted pages
  python extract.py --limit 20   # process only 20 pages
  python extract.py --cat REPO   # only one category
  python extract.py --dry        # show what would be extracted without saving
"""

import json
import sys
import time
import re
import requests
from pathlib import Path

BASE_DIR = Path(__file__).parent
cfg = json.loads((BASE_DIR / "config.json").read_text())

TOKEN    = cfg["notion_token"]
IDEAS_DB = cfg["content_ideas_db_id"]
OLLAMA   = "http://localhost:11434/api/generate"
MODEL    = cfg.get("text_model", "gpt-oss:20b")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

EXTRACT_MARKER = "🧠"  # marker in Angle field to detect already-extracted pages

# ── Category-specific extraction prompts ─────────────────────────────────────

PROMPTS = {
    "PROMPT": """Tu reçois le contenu d'un post Instagram (caption + éventuellement le texte lu dans la vidéo ou les slides) censé contenir un ou plusieurs prompts IA.

RÈGLES STRICTES :
1. Si AUCUN prompt réel n'est présent — le post dit juste « commente X pour recevoir le prompt », ou il DÉCRIT un prompt sans le donner — réponds EXACTEMENT ce seul mot : AUCUN_PROMPT
2. Si UN seul prompt est présent : renvoie son TEXTE VERBATIM, nettoyé (retire hashtags, mentions @, emojis de déco, phrases d'accroche du créateur). Ne le résume pas, ne le décris pas.
3. Si PLUSIEURS prompts distincts sont présents : sépare-les EXACTEMENT ainsi —
### Prompt 1 — <titre court>
<texte verbatim du prompt 1>

### Prompt 2 — <titre court>
<texte verbatim du prompt 2>

Ne donne JAMAIS une description de ce que fait le prompt. Donne le texte qu'on doit coller tel quel dans l'IA.

CONTENU DU POST :
{caption}

RÉSULTAT :""",

    "REPO": """Tu reçois le contenu d'un post Instagram (caption + texte lu dans la vidéo/slides) sur des repos GitHub ou ressources open source.

Si le post ne NOMME aucun repo/ressource réel (juste « commente pour recevoir les liens ») → réponds EXACTEMENT : AUCUN_CONTENU

Sinon, extrais une liste structurée de chaque repo/ressource RÉELLEMENT nommé(e) :
- Nom du repo
- URL GitHub si présente (sinon "URL non mentionnée")
- Ce qu'il fait en 1 phrase

Format : "- **NomRepo** (github.com/...) — Description courte"
Réponds uniquement avec la liste, rien d'autre.

CONTENU DU POST:
{caption}

REPOS EXTRAITS:""",

    "OUTIL": """Tu reçois le contenu d'un post Instagram (caption + texte lu dans la vidéo/slides) sur un ou plusieurs outils/apps/services IA.

Si aucun outil n'est RÉELLEMENT nommé (juste « commente pour le lien ») → réponds EXACTEMENT : AUCUN_CONTENU

Sinon, pour CHAQUE outil nommé :
- **Nom de l'outil** — ce qu'il fait (1-2 phrases) — lien si mentionné

Réponds direct et factuel, une puce par outil.

CONTENU DU POST:
{caption}

OUTIL(S) EXTRAIT(S):""",

    "WORKFLOW": """Tu reçois le contenu d'un post Instagram (caption + texte lu dans la vidéo/slides) décrivant un workflow ou une stack d'outils.

Si le post ne décrit aucun workflow/stack RÉEL (juste « commente pour recevoir le guide ») → réponds EXACTEMENT : AUCUN_CONTENU

Sinon, extrais le workflow en étapes numérotées claires.
Pour chaque étape : outil utilisé + ce qu'il fait.
Si c'est une stack, liste les outils avec leur rôle.
Maximum 10 étapes. Sois concis et actionnable.

CONTENU DU POST:
{caption}

WORKFLOW EXTRAIT:""",

    "ASTUCE": """Tu reçois le contenu d'un post Instagram (caption + texte lu dans la vidéo/slides) avec une astuce ou tip.

Si aucune astuce concrète n'est donnée (juste « commente pour l'astuce ») → réponds EXACTEMENT : AUCUN_CONTENU

Sinon, extrais l'astuce en 1-3 phrases maximum, directement actionnables.
Pas d'intro, pas de conclusion, pas d'emojis de déco.
Commence directement par l'astuce.

CONTENU DU POST:
{caption}

ASTUCE EXTRAITE:""",

    "TUTO": """Tu reçois le contenu d'un post Instagram (caption + texte lu dans la vidéo/slides) qui explique comment faire quelque chose.

Si le post ne montre AUCUNE étape réelle (juste « commente pour le tuto complet ») → réponds EXACTEMENT : AUCUN_CONTENU

Sinon, extrais les étapes clés en liste numérotée.
Chaque étape = 1 phrase claire et actionnable.
Maximum 8 étapes. Garde uniquement l'essentiel.

CONTENU DU POST:
{caption}

ÉTAPES EXTRAITES:""",

    "VIDÉO IDEA": """Tu reçois le contenu d'un post Instagram (caption + texte lu dans la vidéo/slides) avec une idée de contenu vidéo à réutiliser.
Extrais :
1. HOOK : l'accroche en 1 phrase percutante
2. ANGLE : le point de vue / approche unique
3. STRUCTURE : les parties principales du contenu (3 max)

Réponds en format court, 5-8 lignes max.

CAPTION:
{caption}

IDÉE EXTRAITE:""",

    "INSPIRATION": """Tu reçois la caption d'un post Instagram de référence visuelle/esthétique.
Extrais en 3-5 mots-clés : style, ambiance, palette, émotion.
Puis 1 phrase décrivant l'usage créatif de cette référence.

CAPTION:
{caption}

INSPIRATION:""",

    # Generic fallback for user-generated categories that don't match the ones
    # above (the pipeline now derives categories from each person's own saves).
    "DEFAULT": """Tu reçois le contenu d'un post Instagram (caption + éventuellement le texte lu dans la vidéo ou les slides).

Si le post ne donne aucun contenu réel (juste « commente pour recevoir »), réponds EXACTEMENT : AUCUN_CONTENU

Sinon, extrais l'essentiel utile et actionnable en quelques puces courtes : ce qu'il faut retenir, faire, ou réutiliser. Reste factuel, pas d'intro ni de conclusion, garde le contenu concret (noms, étapes, liens, chiffres) verbatim quand il y en a.

CONTENU DU POST:
{caption}

EXTRAIT:""",
}

# ── Ollama extraction ─────────────────────────────────────────────────────────

def extract_with_ollama(category: str, caption: str, url: str) -> str:
    """Call Ollama to extract structured content from a post caption."""
    prompt_template = PROMPTS.get(category, PROMPTS["DEFAULT"])
    full_caption = caption
    if url:
        full_caption = f"{caption}\n\nURL: {url}"

    prompt = prompt_template.format(caption=full_caption[:3000])

    for attempt in range(3):
        try:
            r = requests.post(OLLAMA, json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                # gpt-oss est un modèle à raisonnement : il faut un budget large
                # sinon la réflexion interne consomme tout et la sortie est vide/tronquée.
                "options": {"temperature": 0.2, "num_predict": 1800},
            }, timeout=180)
            r.raise_for_status()
            result = r.json().get("response", "").strip()
            if result:
                return result
            # Empty response — retry
            time.sleep(1)
        except requests.exceptions.ConnectionError:
            print("  ⚠ Ollama non disponible — lance 'ollama serve' dans un terminal")
            return ""
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            print(f"  ⚠ Ollama error: {e}")
            return ""
    return ""

# ── Notion helpers ─────────────────────────────────────────────────────────────

def notion_request(method: str, url: str, json_body: dict = None, retries: int = 4):
    """Resilient Notion API call with retry on timeout/5xx/429."""
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=HEADERS, json=json_body, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 3))
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            return r
        except (requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError):
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return r


def fetch_pages(category: str = None, limit: int = 500,
                force: bool = False, noninsp: bool = False) -> list:
    """Fetch Content Ideas pages.
      force=False  → only pages not yet extracted (Angle lacks the 🧠 marker)
      force=True   → all pages, even already-extracted (re-extract)
      category     → keep only pages whose TRUE category (from title) matches
      noninsp      → keep only pages whose TRUE category != INSPIRATION
    Category filtering is done in Python via get_category_from_name so it
    correctly handles old mixed-case nomenclature ([Tutoriel], [Prompt]...).
    """
    raw = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if not force:
            body["filter"] = {
                "property": "Angle",
                "rich_text": {"does_not_contain": EXTRACT_MARKER}
            }
        if cursor:
            body["start_cursor"] = cursor

        r = notion_request("POST",
            f"https://api.notion.com/v1/databases/{IDEAS_DB}/query",
            body)
        r.raise_for_status()
        data = r.json()
        raw.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.2)

    # Python-side category filtering (reliable across nomenclatures)
    pages = []
    for pg in raw:
        titles = pg.get("properties", {}).get("Name", {}).get("title", [])
        name = titles[0]["text"]["content"] if titles else ""
        cat = get_category_from_name(name)
        if category and cat != category:
            continue
        if noninsp and cat == "INSPIRATION":
            continue
        pages.append(pg)
        if len(pages) >= limit:
            break
    return pages


def get_prop_text(props, key) -> str:
    p = props.get(key, {})
    if "rich_text" in p:
        items = p["rich_text"]
        return items[0]["text"]["content"] if items else ""
    if "title" in p:
        items = p["title"]
        return items[0]["text"]["content"] if items else ""
    if "url" in p:
        return p["url"] or ""
    return ""


def get_category_from_name(name: str) -> str:
    """Detect the true category from a title prefix — handles old (mixed-case
    sub-type) and new (uppercase) nomenclatures. Case-insensitive keyword map."""
    m = re.match(r'\[([^\]]+)\]', name)
    if not m:
        return "INSPIRATION"
    p = m.group(1).strip().upper()
    # INSPIRATION first: "Inspiration Vidéo/Mood/Image" must NOT fall into VIDÉO IDEA
    if "INSPIRATION" in p:
        return "INSPIRATION"
    if "PROMPT" in p:
        return "PROMPT"
    if "REPO" in p:
        return "REPO"
    if "OUTIL" in p:
        return "OUTIL"
    if "WORKFLOW" in p:
        return "WORKFLOW"
    if "ASTUCE" in p:
        return "ASTUCE"
    if "TUTO" in p:  # TUTO, TUTORIEL
        return "TUTO"
    if "VIDÉO" in p or "VIDEO" in p or "IDÉE" in p:
        return "VIDÉO IDEA"
    # Unknown prefix → a user-generated category. Keep its real name so --cat
    # filtering and the extraction prompt selection work on the actual scheme.
    return p


def strip_prefix(name: str) -> str:
    """Return the title without its [Category] prefix, for display."""
    return re.sub(r'^\[[^\]]+\]\s*', '', name)


def get_page_body_text(page_id: str) -> str:
    """Fetch page body blocks to get the original caption."""
    r = notion_request("GET",
        f"https://api.notion.com/v1/blocks/{page_id}/children")
    if r.status_code != 200:
        return ""
    text = ""
    for block in r.json().get("results", []):
        if block.get("type") == "paragraph":
            for t in block["paragraph"].get("rich_text", []):
                text += t.get("text", {}).get("content", "")
    return text


def get_stored_bundle(page_id: str) -> str:
    """Return the media bundle already stored in the '🎬 Contenu du média' toggle
    (from a previous enrichment), so re-extraction doesn't re-download the media."""
    r = notion_request("GET", f"https://api.notion.com/v1/blocks/{page_id}/children")
    if r.status_code != 200:
        return ""
    for blk in r.json().get("results", []):
        if blk.get("type") == "toggle" and blk.get("toggle", {}).get("rich_text"):
            head = blk["toggle"]["rich_text"][0].get("text", {}).get("content", "")
            if "🎬" in head:
                kids = notion_request("GET", f"https://api.notion.com/v1/blocks/{blk['id']}/children")
                if kids.status_code != 200:
                    return ""
                out = ""
                for k in kids.json().get("results", []):
                    if k.get("type") == "paragraph":
                        for t in k["paragraph"].get("rich_text", []):
                            out += t.get("text", {}).get("content", "")
                return out
    return ""


def update_page_with_extraction(page_id: str, extraction: str, old_angle: str,
                                media_bundle: str = ""):
    """Update Notion page: Angle = extracted content, add callout block.
    If media_bundle is provided (from enrichment), store it in a collapsible
    toggle so the raw OCR/transcript is preserved and re-readable by Claude."""
    angle_content = f"{EXTRACT_MARKER} {extraction[:1990]}"

    # Update Angle property
    r = notion_request("PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": {
            "Angle": {"rich_text": [{"text": {"content": angle_content}}]}
        }})
    if r.status_code not in (200, 201):
        print(f"  ⚠ Notion patch error {r.status_code}: {r.text[:100]}")
        return False

    # Idempotence: remove previous extraction blocks (🧠 callout / 🎬 toggle) so
    # re-runs (--force) don't stack duplicates.
    existing = notion_request("GET", f"https://api.notion.com/v1/blocks/{page_id}/children")
    if existing.status_code == 200:
        for blk in existing.json().get("results", []):
            t = blk.get("type")
            is_old = (
                (t == "callout" and blk.get("callout", {}).get("icon", {}).get("emoji") == "🧠")
                or (t == "toggle" and blk.get("toggle", {}).get("rich_text")
                    and "🎬" in blk["toggle"]["rich_text"][0].get("text", {}).get("content", ""))
            )
            if is_old:
                notion_request("DELETE", f"https://api.notion.com/v1/blocks/{blk['id']}")

    # Prepend a callout block with the extraction
    children = [{
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": extraction[:1990]}}],
            "icon": {"type": "emoji", "emoji": "🧠"},
            "color": "blue_background"
        }
    }]
    # Preserve the raw media reading in a collapsible toggle (OCR + transcript)
    if media_bundle:
        chunks = [media_bundle[i:i+1900] for i in range(0, min(len(media_bundle), 5700), 1900)]
        children.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "🎬 Contenu du média (lu par IA)"}}],
                "children": [{
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": c}}]}
                } for c in chunks]
            }
        })
    r2 = notion_request("PATCH",
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        {"children": children})
    return r2.status_code in (200, 201)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    limit = 500
    category = None
    dry_run = "--dry" in args
    if dry_run:
        args.remove("--dry")
    force = "--force" in args
    if force:
        args.remove("--force")
    noninsp = "--noninsp" in args
    if noninsp:
        args.remove("--noninsp")
    enrich = "--enrich" in args
    if enrich:
        args.remove("--enrich")
    enrich_all = "--enrich-all" in args
    if enrich_all:
        args.remove("--enrich-all")
        enrich = True
    # --reextract : ré-applique l'extraction à TOUTES les pages (ex: après avoir
    # changé un prompt d'extraction), sans sauter les pages déjà 🧠. Réutilise les
    # bundles média stockés pour les visuels, la caption pour le reste.
    reextract = "--reextract" in args
    if reextract:
        args.remove("--reextract")

    if "--limit" in args:
        idx = args.index("--limit")
        limit = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if "--cat" in args:
        idx = args.index("--cat")
        category = args[idx + 1].upper()
        args = args[:idx] + args[idx + 2:]

    mode = []
    if force: mode.append("FORCE re-extract")
    if noninsp: mode.append("non-INSPIRATION only")
    if enrich_all: mode.append("ENRICH-ALL (vision+audio)")
    elif enrich: mode.append("ENRICH thin-caption visuals")
    if category: mode.append(f"cat={category}")
    mode_str = f" [{', '.join(mode)}]" if mode else ""

    # Enrichment setup: build shortcode→media map from Instagram Saves DB
    saves_map = {}
    ig_session = None
    if enrich:
        import enrich as E
        print("Enrichissement activé — construction de la map média...")
        saves_map = E.build_saves_map(TOKEN, cfg["instagram_saves_db_id"])
        ig_session = E.make_ig_session()
        print(f"  {len(saves_map)} posts mappés (média récupérable)\n")

    print(f"{'[DRY RUN] ' if dry_run else ''}Fetching pages to extract{mode_str}...")
    pages = fetch_pages(category=category, limit=limit, force=force, noninsp=noninsp)
    print(f"Found {len(pages)} pages to process\n")

    if not pages:
        print("Nothing to do — all pages already extracted.")
        return

    counts = {"ok": 0, "skip": 0, "error": 0, "enriched": 0}

    for i, page in enumerate(pages, 1):
        props = page.get("properties", {})
        page_id = page["id"]
        name = get_prop_text(props, "Name")
        cat = get_category_from_name(name)
        angle = get_prop_text(props, "Angle") or ""

        # Determine the source caption. If the Angle already holds an extraction
        # (marker present) or is too short, read the original caption from the body.
        need_body = enrich or (EXTRACT_MARKER in angle) or len(angle) < 50
        body = get_page_body_text(page_id) if need_body else ""
        if EXTRACT_MARKER in angle or len(angle) < 50:
            caption = body
        else:
            caption = angle

        # Multimodal enrichment: read the actual video/carousel when the caption
        # is thin (or --enrich-all). Appends OCR of slides / audio transcript / on-screen text.
        enriched_tag = ""
        media_bundle = ""
        if enrich:
            import enrich as E
            m = re.search(r"instagram\.com/(?:p|reel)/([^/\s)]+)", body or "")
            info = saves_map.get(m.group(1)) if m else None
            cap_l = (caption or "").lower()
            thin = len((caption or "").strip()) < 200
            # "Appât à commentaire" : la valeur est dans le média, pas la caption.
            bait = any(k in cap_l for k in (
                "comment ", "commente", "commentez", "dm ", "send you", "i'll send",
                "je t'envoie", "je t’envoie", "lien en bio", "link in bio", "drop a comment",
                "reply ", "réponds", "get the link", "get the prompt", "full guide",
            ))
            is_visual = bool(info and info.get("media_id")
                             and info.get("type") in ("Reel", "Carousel", "Post", "IGTV"))
            will_enrich = is_visual and (enrich_all or thin or bait)

            # Efficiency: when re-processing (force) an already-extracted page that
            # won't be enriched, skip it — its text extraction is already fine.
            # (Sauf --reextract : on veut ré-appliquer le nouveau prompt partout.)
            if force and (EXTRACT_MARKER in angle) and not will_enrich and not reextract:
                counts["skip"] += 1
                continue

            if will_enrich:
                # Réutilise le bundle déjà lu (toggle) si présent → pas de re-download.
                extra = get_stored_bundle(page_id)
                if not extra:
                    extra = E.enrich_media(info["media_id"], ig_session)
                if extra and len(extra) > 20:
                    caption = f"{caption}\n\n=== CONTENU DU MÉDIA (lu par IA) ===\n{extra}"
                    media_bundle = extra
                    enriched_tag = " 👁"
                    counts["enriched"] += 1

        if not caption or len(caption.strip()) < 20:
            print(f"[{i:3}/{len(pages)}] ⊘ skip (no content) — {strip_prefix(name)[:55]}")
            counts["skip"] += 1
            continue

        extraction = extract_with_ollama(cat, caption, "")

        if not extraction:
            counts["error"] += 1
            print(f"[{i:3}/{len(pages)}] ⚠ [{cat:<12}] Ollama vide — {strip_prefix(name)[:45]}")
            continue

        # Teaser DM-gated : le modèle a signalé qu'il n'y a pas de contenu réel
        # (AUCUN_PROMPT / AUCUN_CONTENU / AUCUN_REPO …).
        if extraction.strip().upper().replace("*", "").startswith("AUCUN_"):
            extraction = ("⏳ Contenu non présent dans le post — le créateur l'envoie en DM "
                          "après commentaire. Rien d'exploitable ici.")
            tag = "⏳"
        else:
            tag = f"★{enriched_tag}"

        print(f"[{i:3}/{len(pages)}] {tag} [{cat:<12}] {strip_prefix(name)[:48]}")
        if dry_run:
            print(f"    → {extraction[:120]}...")
            counts["ok"] += 1
            continue

        ok = update_page_with_extraction(page_id, extraction, angle, media_bundle)
        if ok:
            counts["ok"] += 1
        else:
            counts["error"] += 1

        time.sleep(0.5)  # Ollama + Notion rate limit

    print(f"\n{'─'*60}")
    print(f"Extracted: {counts['ok']} | Skipped: {counts['skip']} | "
          f"Errors: {counts['error']} | 👁 Enrichis: {counts['enriched']}")


if __name__ == "__main__":
    main()
