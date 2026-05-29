#!/usr/bin/env python3
"""index_all.py — bulk-index the corpus via the EXISTING index_document tool.

This drives the same `index_document` MCP tool the agent uses (same chunking,
same Memory/FAISS writes, same Ollama embeddings) over every .md file in the
corpus folder — but WITHOUT the agent's Perception/Decision loop. That loop
makes two LLM chat calls per iteration and is capped at 20 iterations, so a
single "index every .md" agent run is slow and can't even finish 27 files.
Calling the tool directly is bulk loading: no LLM reasoning, no iteration cap.

It does NOT re-implement retrieval/chunking/embedding — it calls the real MCP
server tool over stdio, exactly as the agent does.

Usage (from RAG-Finance/, venv active):
    python index_all.py                 # index sandbox/finance/*.md
    python index_all.py --dir finance   # explicit subfolder under sandbox/
    python index_all.py --fresh         # wipe state/ first (avoid duplicates)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gateway import ensure_gateway

BASE = Path(__file__).resolve().parent
MCP_SERVER = BASE / "mcp_server.py"


def _wipe_state() -> None:
    state = BASE / "state"
    for name in ("memory.json", "index.faiss", "index_ids.json"):
        p = state / name
        if p.exists():
            p.unlink()
    print("[fresh] wiped state/")


async def run(corpus_dir: str) -> None:
    ensure_gateway()  # so the embed endpoint (Ollama) is up
    folder = BASE / "sandbox" / corpus_dir
    files = sorted(folder.glob("*.md"))
    if not files:
        print(f"[done] no .md files under sandbox/{corpus_dir}/")
        return

    print(f"[start] indexing {len(files)} files from sandbox/{corpus_dir}/")
    print("-" * 70)
    server_params = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER)])
    total_chunks = 0
    failures = 0

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for i, f in enumerate(files, 1):
                rel = f"{corpus_dir}/{f.name}"
                try:
                    res = await session.call_tool("index_document", {"path": rel})
                    text = "".join(getattr(c, "text", "") or "" for c in res.content)
                    n = None
                    try:
                        n = json.loads(text).get("chunks_indexed")
                    except Exception:
                        pass
                    if n is not None:
                        total_chunks += n
                        print(f"[{i}/{len(files)}] {f.name}  → {n} chunks")
                    else:
                        print(f"[{i}/{len(files)}] {f.name}  → {text[:100]}")
                except Exception as e:
                    failures += 1
                    print(f"[{i}/{len(files)}] {f.name}  ERROR: {type(e).__name__}: {e}")

    print("-" * 70)
    print(f"[summary] files={len(files)}  chunks≈{total_chunks}  failures={failures}")
    print("[next] verify with:  python index_status.py")


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-index a corpus folder via index_document.")
    ap.add_argument("--dir", default="finance", help="subfolder under sandbox/ holding the .md files")
    ap.add_argument("--fresh", action="store_true", help="wipe state/ before indexing (avoid duplicates)")
    args = ap.parse_args()
    if args.fresh:
        _wipe_state()
    asyncio.run(run(args.dir))


if __name__ == "__main__":
    main()
