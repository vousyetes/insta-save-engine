#!/usr/bin/env python3
"""
Multimodal enrichment of Instagram posts (local, free).

When the value of a post is inside the VIDEO or the carousel SLIDES (not in the
text caption), this module actually "reads" the media:

  Carousel / image  → downloads the slides → OCR via a local Ollama vision model
  Reel / video      → downloads the mp4 → audio transcription (whisper-cli)
                      + OCR of a few frames (on-screen text) via the vision model

It returns an enriched text bundle (caption + slide OCR + transcript + frames)
which is then fed to the text model for category extraction.

Media is fetched from Instagram's private API (media/{id}/info/) with the session
cookies : same mechanism as sync.py.

Config knobs (config.json, all optional):
  "whisper_model_path" : path to a whisper.cpp ggml model. Empty / missing file
                         → audio transcription is skipped (OCR still runs).
  "vision_model"       : Ollama vision model (default "qwen2.5vl:7b").

Import: enrich_media(media_id, session) -> str
Standalone (test): python enrich.py <shortcode>
"""

import json
import base64
import subprocess
import sys
import time
import tempfile
import shutil
import re
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
cfg = json.loads((BASE_DIR / "config.json").read_text())

COOKIES      = cfg.get("instagram_cookies", {})
OLLAMA_GEN   = "http://localhost:11434/api/generate"
VISION_MODEL = cfg.get("vision_model", "qwen2.5vl:7b")
WHISPER_BIN  = cfg.get("whisper_bin", "whisper-cli")
WHISPER_MODEL = str(cfg.get("whisper_model_path", "") or "")

# Guard rails (machine load)
MAX_SLIDES  = 20    # carousel slides to OCR (Instagram's max per post)
MAX_FRAMES  = 8     # video frames to OCR (on-screen text of a reel changes)
IMG_MAXDIM  = 1536  # vision resolution: 768 cut small text AND made qwen2.5vl
                    # hallucinate (invented names). 1536 recovers dense prompts/
                    # boards in full. Proof: one GPT Image 2 board went from
                    # 582 chars (some hallucinated) to 1415 exact chars.
VISION_NUMPREDICT = 1200  # 400 truncated long slides (full prompts)
VISION_TIMEOUT = 180


def whisper_available() -> bool:
    """True only if a whisper binary AND a model file are both present."""
    if not WHISPER_MODEL or not Path(WHISPER_MODEL).exists():
        return False
    return shutil.which(WHISPER_BIN) is not None

# ── Instagram session (same as sync.py) ───────────────────────────────────────

def _fresh_auth() -> tuple[dict, str]:
    """Fresh cookies + user-agent from the instagrapi session (ig_session.json),
    which sync.py refreshes on its own. Falls back to the static cookies in
    config.json.

    Instagram ties the session to the user-agent that created it: reusing the
    fresh sessionid WITH its mobile UA avoids the login redirect loop (stale
    cookies).
    """
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    cookies = dict(COOKIES)
    try:
        sess = json.loads((BASE_DIR / "ig_session.json").read_text())
        ad = sess.get("authorization_data", {})
        if ad.get("sessionid") and ad.get("ds_user_id"):
            cookies["sessionid"] = ad["sessionid"]
            cookies["ds_user_id"] = str(ad["ds_user_id"])
            if sess.get("user_agent"):
                ua = sess["user_agent"]
    except Exception:
        pass
    return cookies, ua


def make_ig_session() -> requests.Session:
    cookies, ua = _fresh_auth()
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    })
    s.cookies.update(cookies)
    if csrf := cookies.get("csrftoken"):
        s.headers["X-CSRFToken"] = csrf
    return s

# ── Fetch media URLs via IG API ───────────────────────────────────────────────

def get_media_info(session: requests.Session, media_id: str) -> dict:
    """Return the full media object (carousel_media / video_versions / image)."""
    # allow_redirects=False: a stale session returns a 302 to /login instead of
    # looping 30 times (TooManyRedirects) and killing the whole extraction run.
    r = session.get(f"https://www.instagram.com/api/v1/media/{media_id}/info/",
                    timeout=15, allow_redirects=False)
    if r.status_code != 200:
        return {}
    items = r.json().get("items", [])
    return items[0] if items else {}


def best_image_url(node: dict) -> str:
    """A medium-sized image URL from a media node."""
    cands = node.get("image_versions2", {}).get("candidates", [])
    if not cands:
        return ""
    # medium size (not the thumbnail, not the 4K)
    cands = sorted(cands, key=lambda c: c.get("width", 0))
    return cands[len(cands) // 2].get("url", "")


def collect_media_urls(media: dict) -> dict:
    """Return {'type': 'video'|'images', 'video': url, 'images': [urls]}."""
    mtype = media.get("media_type", 1)
    # Carousel (8): several slides, each an image or a video
    if mtype == 8:
        imgs = []
        for node in media.get("carousel_media", [])[:MAX_SLIDES]:
            # a video slide → take its cover image (OCR is enough here)
            u = best_image_url(node)
            if u:
                imgs.append(u)
        return {"type": "images", "video": "", "images": imgs}
    # Video / Reel (2)
    if mtype == 2:
        vv = media.get("video_versions", [])
        vurl = vv[0].get("url", "") if vv else ""
        return {"type": "video", "video": vurl, "images": []}
    # Single image (1)
    u = best_image_url(media)
    return {"type": "images", "video": "", "images": [u] if u else []}

# ── Download ──────────────────────────────────────────────────────────────────

def download(url: str, dest: Path, session: requests.Session) -> bool:
    try:
        r = session.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            return False
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return dest.stat().st_size > 0
    except Exception:
        return False

# ── Vision (Ollama) ───────────────────────────────────────────────────────────

def downscale(img_path: Path) -> Path:
    """Shrink the image to lighten vision (ffmpeg). Returns the shrunk path."""
    out = img_path.with_suffix(".small.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(img_path),
             "-vf", f"scale='min({IMG_MAXDIM},iw)':-1", str(out)],
            check=True, timeout=60,
        )
        return out if out.exists() else img_path
    except Exception:
        return img_path


def vision_read(img_path: Path, instruction: str) -> str:
    """Have the Ollama vision model read an image → text."""
    small = downscale(img_path)
    try:
        b64 = base64.b64encode(small.read_bytes()).decode()
    except Exception:
        return ""
    try:
        r = requests.post(OLLAMA_GEN, json={
            "model": VISION_MODEL,
            "prompt": instruction,
            "images": [b64],
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": VISION_NUMPREDICT},
        }, timeout=VISION_TIMEOUT)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"[vision error: {e}]"


SLIDE_INSTRUCTION = (
    "You are looking at one slide of an Instagram carousel/post. Transcribe WORD "
    "FOR WORD ALL the visible text, including small text, full paragraphs, prompts, "
    "code, lists, numbered steps, values (hex colors, numbers), tool names and links. "
    "Do NOT summarize, do NOT skip anything. If a block looks like a copyable AI "
    "prompt, reproduce it IN FULL as-is. "
    "ANTI-INVENTION RULE: write only what is actually legible; if a word is unreadable "
    "put [illegible], never invent names or content. If the slide is mostly visual "
    "(diagram, screenshot, product), add at most 1 factual sentence of description."
)

FRAME_INSTRUCTION = (
    "You are looking at one frame of an Instagram video. Transcribe WORD FOR WORD all "
    "the text shown on screen (title, burned-in subtitles, hook, steps, tool names, "
    "numbers, code, links). Do NOT describe the scene or the person. Invent nothing. "
    "If there is no legible text, answer exactly: NONE."
)

# ── Audio (whisper-cli) ───────────────────────────────────────────────────────

def transcribe(video_path: Path, workdir: Path) -> str:
    """Extract audio and transcribe it with whisper-cli. '' if no speech, or if
    no whisper model is configured (light mode)."""
    if not whisper_available():
        return ""
    wav = workdir / "audio.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
            check=True, timeout=120,
        )
    except Exception:
        return ""
    if not wav.exists() or wav.stat().st_size < 1000:
        return ""
    out_prefix = workdir / "transcript"
    try:
        subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(wav),
             "-l", "auto", "-np", "-nt", "-otxt", "-of", str(out_prefix)],
            check=True, timeout=600, capture_output=True,
        )
    except Exception:
        return ""
    txt = out_prefix.with_suffix(".txt")
    if txt.exists():
        return txt.read_text(errors="ignore").strip()
    return ""


def sample_frames(video_path: Path, workdir: Path, n: int = MAX_FRAMES) -> list:
    """Extract n representative frames (on-screen text)."""
    pattern = str(workdir / "frame_%02d.jpg")
    try:
        # 'thumbnail' picks representative frames per batch
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
             "-vf", f"thumbnail=100,scale={IMG_MAXDIM}:-1", "-frames:v", str(n),
             "-vsync", "vfr", pattern],
            check=True, timeout=120,
        )
    except Exception:
        return []
    return sorted(workdir.glob("frame_*.jpg"))

# ── Orchestration ─────────────────────────────────────────────────────────────

def enrich_media(media_id: str, session: requests.Session = None) -> str:
    """Download and read a post's media → enriched text bundle ('' if nothing)."""
    session = session or make_ig_session()
    media = get_media_info(session, media_id)
    if not media:
        return ""
    urls = collect_media_urls(media)
    workdir = Path(tempfile.mkdtemp(prefix="ig_enrich_"))
    parts = []
    try:
        if urls["type"] == "images" and urls["images"]:
            for i, u in enumerate(urls["images"], 1):
                dest = workdir / f"slide_{i:02d}.jpg"
                if download(u, dest, session):
                    txt = vision_read(dest, SLIDE_INSTRUCTION)
                    if txt and "[vision error" not in txt:
                        parts.append(f"[Slide {i}]\n{txt}")

        elif urls["type"] == "video" and urls["video"]:
            vid = workdir / "video.mp4"
            if download(urls["video"], vid, session):
                # 1) spoken audio
                tr = transcribe(vid, workdir)
                if tr and len(tr) > 15:
                    parts.append(f"[Audio transcript]\n{tr[:6000]}")
                # 2) on-screen text (frames) : deduplicated, text only
                frames = sample_frames(vid, workdir)
                seen, frame_txt = set(), []
                for fr in frames:
                    t = vision_read(fr, FRAME_INSTRUCTION)
                    t = t.strip()
                    if not t or t.upper() == "NONE" or "[vision error" in t or len(t) < 5:
                        continue
                    key = t.lower()[:40]
                    if key in seen:
                        continue
                    seen.add(key)
                    frame_txt.append(t)
                if frame_txt:
                    joined = "\n".join(frame_txt)
                    parts.append(f"[On-screen text]\n{joined[:5000]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return "\n\n".join(parts).strip()

# ── Standalone test ───────────────────────────────────────────────────────────

def build_saves_map(notion_token: str, saves_db: str) -> dict:
    """{shortcode: {'media_id', 'type', 'url'}} from the Instagram Saves DB."""
    H = {"Authorization": f"Bearer {notion_token}",
         "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    smap, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{saves_db}/query",
                          headers=H, json=body, timeout=20)
        r.raise_for_status()
        d = r.json()
        for p in d.get("results", []):
            props = p.get("properties", {})
            url = props.get("URL", {}).get("url", "") or ""
            m = re.search(r"/p/([^/]+)/", url) or re.search(r"/reel/([^/]+)/", url)
            if not m:
                continue
            mid_rt = props.get("Media ID", {}).get("rich_text", [])
            mid = mid_rt[0]["text"]["content"] if mid_rt else ""
            tsel = props.get("Type", {}).get("select")
            typ = tsel["name"] if tsel else ""
            smap[m.group(1)] = {"media_id": mid, "type": typ, "url": url}
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")
    return smap


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else None
    smap = build_saves_map(cfg["notion_token"], cfg["instagram_saves_db_id"])
    print(f"Saves map: {len(smap)} posts")
    if not whisper_available():
        print("(whisper not configured : reels will be OCR-only, no audio transcript)")
    if not sc:
        # grab one carousel and one reel at random for a test
        car = next((k for k, v in smap.items() if v["type"] == "Carousel" and v["media_id"]), None)
        reel = next((k for k, v in smap.items() if v["type"] == "Reel" and v["media_id"]), None)
        print(f"Example carousel: {car}  |  reel: {reel}")
        for label, code in [("CAROUSEL", car), ("REEL", reel)]:
            if not code:
                continue
            print(f"\n{'='*60}\n{label} : {code}\n{'='*60}")
            out = enrich_media(smap[code]["media_id"])
            print(out[:1500] if out else "(nothing extracted)")
    else:
        info = smap.get(sc)
        if not info:
            print(f"shortcode {sc} not found in Saves")
            sys.exit(1)
        print(f"Type: {info['type']}  media_id: {info['media_id']}")
        out = enrich_media(info["media_id"])
        print(out or "(nothing extracted)")
