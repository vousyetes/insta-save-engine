# sync-instagram

Run the Insta Save Engine pipeline from Claude Code.

## Usage

```
/sync-instagram          — run the full pipeline (sync → classify → extract)
/sync-instagram light    — sync + classify only (no local AI extraction)
/sync-instagram status   — how many unprocessed saves are waiting
```

## full

When the user runs `/sync-instagram`:

1. Run `.venv/bin/python sync.py` (fetch new saved posts into the Instagram Saves DB).
2. Run `.venv/bin/python ideate.py` (classify Status=New posts into Content Ideas).
3. Run `.venv/bin/python extract.py --enrich --limit 100` (local AI extraction; reads
   the media of new reels/carousels). Requires Ollama running.
4. Report the counts from each step. If Ollama is unreachable, say so and note that
   steps 1–2 still ran; extraction will catch up on the next run.

## light

Same as `full` but skip step 3 (no Ollama needed).

## status

Run `.venv/bin/python query.py` and report the per-category summary of Content Ideas.
