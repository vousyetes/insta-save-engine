#!/usr/bin/env python3
"""Search index/index.jsonl locally, without network access or an LLM."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path


INDEX_FILE = Path(__file__).resolve().parent / "index" / "index.jsonl"
WORD_RE = re.compile(r"[a-z0-9][a-z0-9.+#_-]*", re.IGNORECASE)
STOPWORDS = {
    "a", "all", "and", "de", "des", "du", "et", "for", "in", "la", "le",
    "les", "mes", "of", "on", "sur", "the", "to", "un", "une",
}


def normalize(text: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(character)
    ).lower()


def words(text: str) -> list[str]:
    return [word for word in WORD_RE.findall(normalize(text)) if len(word) > 1 and word not in STOPWORDS]


def score(record: dict, question: str) -> int:
    terms = words(question)
    if not terms:
        return 0
    zones = {
        "title": normalize(record.get("title", "")),
        "themes": normalize(" ".join(record.get("themes", []))),
        "category": normalize(record.get("category", "")),
        "author": normalize(record.get("author", "")),
        "summary": normalize(record.get("summary", "")),
    }
    weights = {"title": 8, "themes": 7, "category": 5, "author": 3, "summary": 2}
    result = sum(weights[zone] for term in terms for zone, text in zones.items() if term in text)
    phrase = normalize(question).strip()
    if phrase and phrase in " ".join(zones.values()):
        result += 12
    exact_words = words(" ".join(zones.values()))
    result += sum(1 for term in terms if term in exact_words)
    return result


def search(records: list[dict], question: str, limit: int = 40) -> list[dict]:
    ranked = [(score(record, question), record) for record in records]
    ranked = [(value, record) for value, record in ranked if value > 0]
    ranked.sort(key=lambda item: (item[0], item[1].get("date_saved", "")), reverse=True)
    return [record for _, record in ranked[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be greater than zero")
    if not INDEX_FILE.exists():
        raise SystemExit("index/index.jsonl is missing. Run index.py first")
    records = [
        json.loads(line) for line in INDEX_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = search(records, args.question, args.limit)
    if not results:
        print("Nothing found in your saved posts.")
        return 0
    print(f"{len(results)} result(s) for: {args.question}\n")
    for record in results:
        themes = ", ".join(record.get("themes", [])) or "no theme"
        summary = (record.get("summary") or "no summary").replace("\n", " / ")
        print(f"### {record['title']}")
        print(f"@{record.get('author') or 'unknown'} | {record['category']} | {themes}")
        print(summary)
        print(record.get("url") or record.get("notion_url") or "")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
