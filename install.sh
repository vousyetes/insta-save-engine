#!/usr/bin/env bash
#
# install.sh — one-shot setup for Insta Save Engine (macOS, Apple Silicon or Intel).
#
#   ./install.sh          full install  (Python + Ollama models + whisper)
#   ./install.sh --light  light mode    (Python only: sync + classification,
#                                         no local AI extraction, no media reading)
#
# It never touches your secrets: it only creates a venv, installs Python deps,
# pulls the local AI models, downloads a whisper model, and generates the
# launchd file. You still fill in config.json and run the Notion + Instagram
# setup yourself (see the README).

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

LIGHT=0
[[ "${1:-}" == "--light" ]] && LIGHT=1

say()  { printf "\n\033[1m▶ %s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

# ── 1. Python + venv ──────────────────────────────────────────────────────────
say "Python virtual environment"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [[ -z "$PY" ]]; then
  warn "No Python 3 found. Install it first:  brew install python@3.13"
  exit 1
fi
ok "Using $($PY --version)"

if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
  ok "Created .venv"
else
  ok ".venv already exists"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
ok "Python dependencies installed"

# ── 2. config.json ────────────────────────────────────────────────────────────
say "Config file"
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  chmod 600 config.json
  ok "Created config.json from the example (fill it in — see README)"
else
  ok "config.json already exists — left untouched"
fi

# ── 3. ffmpeg (needed for media reading) ──────────────────────────────────────
if [[ $LIGHT -eq 0 ]]; then
  say "ffmpeg (image/video processing)"
  if command -v ffmpeg >/dev/null 2>&1; then
    ok "ffmpeg present"
  elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg && ok "ffmpeg installed"
  else
    warn "ffmpeg missing and Homebrew not found. Install Homebrew, then: brew install ffmpeg"
  fi
fi

# ── 4. Ollama + local models ──────────────────────────────────────────────────
if [[ $LIGHT -eq 0 ]]; then
  say "Ollama + local AI models (~26 GB total, one-time download)"
  if ! command -v ollama >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      brew install ollama && ok "Ollama installed"
    else
      warn "Ollama not found. Install it from https://ollama.com then re-run ./install.sh"
    fi
  else
    ok "Ollama present"
  fi

  if command -v ollama >/dev/null 2>&1; then
    # Make sure the Ollama server is reachable; start it in the background if not.
    if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
      warn "Starting the Ollama server in the background…"
      (ollama serve >/dev/null 2>&1 &) || true
      sleep 4
    fi
    if curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
      ollama pull qwen2.5vl:7b && ok "Vision model ready (qwen2.5vl:7b)"
      ollama pull gpt-oss:20b  && ok "Text model ready (gpt-oss:20b)"
    else
      warn "Could not reach Ollama. Open the Ollama app, then run:"
      warn "  ollama pull qwen2.5vl:7b && ollama pull gpt-oss:20b"
    fi
  fi
fi

# ── 5. whisper.cpp + model (reel audio transcription) ─────────────────────────
if [[ $LIGHT -eq 0 ]]; then
  say "whisper.cpp (reel audio → text)"
  if ! command -v whisper-cli >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      brew install whisper-cpp && ok "whisper-cpp installed"
    else
      warn "whisper-cpp not found and Homebrew missing. Reels will be OCR-only."
    fi
  else
    ok "whisper-cli present"
  fi

  mkdir -p models
  WMODEL="models/ggml-large-v3-turbo-q5_0.bin"
  if [[ ! -f "$WMODEL" ]]; then
    warn "Downloading whisper model (~550 MB)…"
    curl -L --fail -o "$WMODEL" \
      "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin" \
      && ok "Whisper model downloaded" \
      || warn "Whisper model download failed — reels will be OCR-only until you retry."
  else
    ok "Whisper model already present"
  fi
  # Point config.json at the model (only if config.json exists and the file is there).
  if [[ -f "$WMODEL" && -f config.json ]]; then
    python - "$DIR/$WMODEL" <<'PY'
import json, sys
from pathlib import Path
cfg = Path("config.json")
data = json.loads(cfg.read_text())
data["whisper_model_path"] = sys.argv[1]
cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("  \033[32m✓\033[0m whisper_model_path written to config.json")
PY
  fi
fi

# ── 6. launchd file (twice-daily auto-sync, optional) ─────────────────────────
say "Scheduled auto-sync (optional)"
PLIST="com.user.insta-save-engine.plist"
if [[ -f "${PLIST}.template" ]]; then
  sed -e "s#__VENV_PYTHON__#$DIR/.venv/bin/python#g" \
      -e "s#__PROJECT_DIR__#$DIR#g" \
      "${PLIST}.template" > "$PLIST"
  ok "Generated $PLIST (paths filled in)"
  echo "     To enable twice-daily sync (9:00 and 21:00):"
  echo "       cp $PLIST ~/Library/LaunchAgents/"
  echo "       launchctl load ~/Library/LaunchAgents/$PLIST"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
say "Setup complete"
echo "  Next steps (see README for details):"
echo "   1. Fill in config.json (Notion token + parent page id)"
echo "   2. .venv/bin/python setup_notion.py     # creates the 2 Notion databases"
echo "   3. .venv/bin/python setup_auth.py       # logs into Instagram once"
echo "   4. .venv/bin/python sync.py             # first sync"
[[ $LIGHT -eq 1 ]] && echo "   (light mode: skip extract.py — you get sync + classification only)"
