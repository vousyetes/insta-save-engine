# ideas

Search your Content Ideas in Notion, straight from Claude Code.

## Usage

```
/ideas                 — summary: how many ideas per category
/ideas REPO            — all saved repos
/ideas PROMPT claude   — prompts mentioning "claude"
/ideas OUTIL           — all tools
```

Categories: PROMPT, REPO, OUTIL, WORKFLOW, ASTUCE, TUTO, VIDÉO IDEA, INSPIRATION.

## behaviour

Run `.venv/bin/python query.py <args>` with whatever the user passed after `/ideas`,
and show the output. Teasers (posts whose real content is only sent by DM) are hidden
by default; add `--teasers` to include them.
