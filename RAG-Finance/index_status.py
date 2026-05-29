#!/usr/bin/env python3
"""index_status.py — read-only progress view of the FAISS index / memory.

Run this in a SECOND terminal while indexing is in progress. It does not
touch the agent; it only reads state/index_ids.json and state/memory.json.

    python index_status.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent / "state"
CORPUS = Path(__file__).resolve().parent / "sandbox" / "finance"


def _read_json(path: Path):
    # memory.json is rewritten on every chunk; retry briefly on a partial read.
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            time.sleep(0.2)
    return None


def main() -> None:
    ids_path = STATE / "index_ids.json"
    mem_path = STATE / "memory.json"

    vectors = 0
    if ids_path.exists():
        ids = _read_json(ids_path) or []
        vectors = len(ids)

    items = _read_json(mem_path) if mem_path.exists() else []
    items = items or []
    facts = [i for i in items if i.get("kind") == "fact"]

    # group indexed chunks by their source document
    per_source: dict[str, int] = {}
    for f in facts:
        src = (f.get("value") or {}).get("source")
        if src:
            per_source[src] = per_source.get(src, 0) + 1

    total_md = len(list(CORPUS.glob("*.md"))) if CORPUS.exists() else 0

    print(f"vectors in FAISS index : {vectors}")
    print(f"fact chunks in memory  : {len(facts)}")
    print(f"corpus .md files       : {total_md}")
    print(f"files indexed so far   : {len(per_source)} / {total_md}")
    if per_source:
        print("\nper-source chunk counts:")
        for src, n in sorted(per_source.items()):
            name = src.split("finance/")[-1] if "finance/" in src else src
            print(f"  {n:>3}  {name}")


if __name__ == "__main__":
    main()
