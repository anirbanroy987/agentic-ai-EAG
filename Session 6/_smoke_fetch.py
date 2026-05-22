"""Standalone smoke test for crawl4ai via the MCP server's fetcher.
Run:  python _smoke_fetch.py [url]"""
import asyncio
import sys
import time

from mcp_server import _crawl4ai_fetch

URL = sys.argv[1] if len(sys.argv) > 1 else "https://en.wikipedia.org/wiki/Claude_Shannon"

t0 = time.time()
r = asyncio.run(_crawl4ai_fetch(URL))
dt = time.time() - t0
print(f"status        : {r['status']}")
print(f"length_bytes  : {r['length_bytes']}")
print(f"wall_clock_s  : {dt:.1f}")
print(f"first 200 ch  : {r['text'][:200]!r}")
