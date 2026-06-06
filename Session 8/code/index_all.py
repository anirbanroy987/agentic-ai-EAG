#!/usr/bin/env python3
"""index_all.py — bulk-index a sandbox corpus through the MCP `index_document` tool.

Mirrors the RAG-Finance helper of the same name. Drives the existing
`index_document` MCP tool (same chunking, same Memory/FAISS writes,
same Ollama embeddings) over every .md / .txt file in a sandbox
subfolder — but WITHOUT the agent's perception/decision loop. That
loop would make 2 LLM calls per iteration, capped at 20, and a single
"index every file" agent run would never finish a real corpus.
Calling the tool directly is bulk loading: no LLM, no iteration cap.

It does NOT re-implement chunking/embedding — it calls the real MCP
server tool over stdio, exactly as the agent does.

Usage (from Session 8/code/, gateway already running):
    uv run python index_all.py --dir finance_articles
    uv run python index_all.py --dir finance_transcripts --ext .txt
    uv run python index_all.py --dir finance_articles --fresh
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
SANDBOX = BASE / "sandbox"
STATE = BASE / "state"


def _wipe_state() -> None:
    for name in ("memory.json", "index.faiss", "index_ids.json"):
        p = STATE / name
        if p.exists():
            p.unlink()
            print(f"[fresh] removed state/{name}")


async def run(corpus_dir: str, exts: list[str], chunk_size: int, overlap: int) -> None:
    ensure_gateway()  # so the embed endpoint (Ollama) is up
    folder = SANDBOX / corpus_dir
    if not folder.exists():
        print(f"[abort] sandbox/{corpus_dir}/ does not exist")
        return
    files: list[Path] = []
    for ext in exts:
        files.extend(folder.glob(f"*{ext}"))
    files = sorted(set(files))
    if not files:
        print(f"[done] no {exts} files under sandbox/{corpus_dir}/")
        return

    print(f"[start] indexing {len(files)} files from sandbox/{corpus_dir}/ "
          f"(chunk_size={chunk_size}, overlap={overlap})")
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
                    res = await session.call_tool(
                        "index_document",
                        {"path": rel, "chunk_size": chunk_size, "overlap": overlap},
                    )
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
                        print(f"[{i}/{len(files)}] {f.name}  → {text[:120]}")
                except Exception as e:
                    failures += 1
                    print(f"[{i}/{len(files)}] {f.name}  ERROR: {type(e).__name__}: {e}")

    print("-" * 70)
    print(f"[summary] files={len(files)}  chunks≈{total_chunks}  failures={failures}")
    print("[next] verify with: ls state/index.faiss ; uv run python flow.py "
          "\"<a finance question that hits the corpus>\"")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bulk-index a sandbox subfolder via the index_document MCP tool."
    )
    ap.add_argument("--dir", default="finance_articles",
                    help="subfolder under sandbox/ holding the files (default: finance_articles)")
    ap.add_argument("--ext", action="append", default=None,
                    help="file extension to include (repeatable). Default: .md and .txt")
    ap.add_argument("--chunk-size", type=int, default=400,
                    help="words per chunk (default 400)")
    ap.add_argument("--overlap", type=int, default=80,
                    help="word overlap between chunks (default 80)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe state/memory.json + index.faiss + index_ids.json before indexing")
    args = ap.parse_args()
    exts = args.ext if args.ext else [".md", ".txt"]
    if args.fresh:
        _wipe_state()
    asyncio.run(run(args.dir, exts, args.chunk_size, args.overlap))


if __name__ == "__main__":
    main()
