#!/usr/bin/env python3
"""scrape_corpus.py — standalone corpus scraper for the RAG-Finance app.

Reads a list of URLs (one per line) from sources.txt, fetches each one,
extracts the main article text (trafilatura for HTML, pypdf for PDFs), and
saves a slugified Markdown file with a 3-line front matter into the corpus
folder. After this runs, you index the produced .md files through the
agent's EXISTING `index_document` MCP tool — this script never touches the
agent, its loop, its MCP tools, embeddings, chunking, or memory.

Behaviour (all required by spec):
  - respects robots.txt (per-host, cached);
  - rate-limits at >= 2s between network requests;
  - logs every failure to skipped.txt instead of crashing;
  - never lets one bad URL abort the whole run;
  - prints a summary at the end.

Usage:
    python scrape_corpus.py
    python scrape_corpus.py --sources sources.txt --out sandbox/finance
    python scrape_corpus.py --delay 3 --limit 10 --overwrite

Dependencies (see requirements-app.txt): httpx, trafilatura, pypdf.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

# Force UTF-8 stdout so the summary (which may contain article titles with
# non-Latin-1 characters) never dies on a Windows cp1252 console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import httpx
    import trafilatura
    from pypdf import PdfReader
except ImportError as e:  # pragma: no cover - dependency guard
    print(
        f"[fatal] missing dependency: {e.name}. "
        "Install with:  uv pip install -r requirements-app.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; RAG-Finance-CorpusBot/1.0; "
    "educational personal-finance corpus; +https://example.invalid/bot)"
)
MIN_DELAY = 2.0  # hard floor — be polite regardless of --delay


# ── helpers ──────────────────────────────────────────────────────────────────

def slugify(text: str, fallback: str = "document", maxlen: int = 80) -> str:
    """Lowercase, hyphenated, filesystem-safe slug."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    text = text[:maxlen].strip("-")
    return text or fallback


def slug_from_url(url: str) -> str:
    p = urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg]
    base = parts[-1] if parts else p.netloc
    base = re.sub(r"\.(html?|php|aspx?|pdf)$", "", base, flags=re.I)
    host = re.sub(r"^www\.", "", p.netloc)
    return slugify(f"{host}-{base}" if base else host)


def unique_path(out_dir: Path, slug: str) -> Path:
    candidate = out_dir / f"{slug}.md"
    n = 2
    while candidate.exists():
        candidate = out_dir / f"{slug}-{n}.md"
        n += 1
    return candidate


def read_sources(path: Path) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        urls.append(line)
    return urls


class RobotsCache:
    """Per-host robots.txt cache. Fail-open: if robots can't be fetched, we
    allow (common crawler convention) but note it."""

    def __init__(self, user_agent: str, timeout: float):
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._cache:
            rp = RobotFileParser()
            robots_url = f"{host}/robots.txt"
            try:
                resp = httpx.get(
                    robots_url,
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": self.user_agent},
                )
                if resp.status_code >= 400:
                    self._cache[host] = None  # treat as allow-all
                else:
                    rp.parse(resp.text.splitlines())
                    self._cache[host] = rp
            except Exception:
                self._cache[host] = None  # fail open
        rp = self._cache[host]
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)


def front_matter(url: str, title: str) -> str:
    """A 3-field YAML front matter block."""
    safe_title = (title or "Untitled").replace("\n", " ").strip()
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        "---\n"
        f"source_url: {url}\n"
        f"fetched_at: {fetched}\n"
        f'title: "{safe_title.replace(chr(34), chr(39))}"\n'
        "---\n\n"
    )


def is_pdf(url: str, content_type: str) -> bool:
    return url.lower().endswith(".pdf") or "application/pdf" in content_type.lower()


def extract_pdf(data: bytes) -> tuple[str, str]:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    title = ""
    try:
        meta = reader.metadata
        if meta and meta.title:
            title = str(meta.title)
    except Exception:
        pass
    return text, title


def extract_html(html: str) -> tuple[str, str]:
    """Return (markdown_body, title) via trafilatura."""
    body = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
        output_format="markdown",
    ) or ""
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and getattr(meta, "title", None):
            title = meta.title
    except Exception:
        pass
    return body, title


# ── main scrape loop ──────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape a URL list into Markdown for the RAG corpus.")
    ap.add_argument("--sources", default="sources.txt", help="file with one URL per line")
    ap.add_argument("--out", default="sandbox/finance", help="output folder for .md files")
    ap.add_argument("--delay", type=float, default=MIN_DELAY, help="seconds between requests (>= 2)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N URLs (0 = all)")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-request timeout (s)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--overwrite", action="store_true", help="re-fetch URLs even if a slug file exists")
    ap.add_argument("--min-chars", type=int, default=200, help="skip pages with less extracted text than this")
    args = ap.parse_args()

    delay = max(MIN_DELAY, args.delay)
    base = Path(__file__).resolve().parent
    sources_path = (base / args.sources).resolve() if not Path(args.sources).is_absolute() else Path(args.sources)
    out_dir = (base / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    skipped_path = out_dir / "skipped.txt"

    if not sources_path.exists():
        print(f"[fatal] sources file not found: {sources_path}", file=sys.stderr)
        raise SystemExit(1)

    urls = read_sources(sources_path)
    if args.limit > 0:
        urls = urls[: args.limit]
    if not urls:
        print("[done] no URLs to scrape.")
        return

    robots = RobotsCache(args.user_agent, args.timeout)
    skipped: list[tuple[str, str]] = []
    saved = 0
    headers = {"User-Agent": args.user_agent, "Accept": "*/*"}

    print(f"[start] {len(urls)} URLs → {out_dir}  (delay {delay:.1f}s)")
    print(f"{'─' * 70}")

    with httpx.Client(timeout=args.timeout, follow_redirects=True, headers=headers) as client:
        for i, url in enumerate(urls, 1):
            prefix = f"[{i}/{len(urls)}]"
            try:
                # 1. robots.txt
                if not robots.allowed(url):
                    print(f"{prefix} ROBOTS-BLOCKED  {url}")
                    skipped.append((url, "blocked by robots.txt"))
                    continue

                # 2. skip if already present (unless --overwrite)
                slug = slug_from_url(url)
                if not args.overwrite and (out_dir / f"{slug}.md").exists():
                    print(f"{prefix} EXISTS (skip)   {slug}.md")
                    continue

                # 3. fetch
                resp = client.get(url)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")

                # 4. extract
                if is_pdf(url, ctype):
                    body, title = extract_pdf(resp.content)
                else:
                    body, title = extract_html(resp.text)

                body = (body or "").strip()
                if len(body) < args.min_chars:
                    print(f"{prefix} THIN ({len(body)} chars)  {url}")
                    skipped.append((url, f"extracted only {len(body)} chars"))
                    continue

                # 5. write markdown
                if not title:
                    title = slug.replace("-", " ").title()
                if args.overwrite:
                    dest = out_dir / f"{slug}.md"
                else:
                    dest = unique_path(out_dir, slug)
                dest.write_text(front_matter(url, title) + body + "\n", encoding="utf-8")
                saved += 1
                print(f"{prefix} SAVED  {dest.name}  ({len(body)} chars)")

            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                print(f"{prefix} HTTP {code}  {url}")
                skipped.append((url, f"HTTP {code}"))
            except Exception as e:
                print(f"{prefix} ERROR  {url}  ({type(e).__name__}: {e})")
                skipped.append((url, f"{type(e).__name__}: {e}"))
            finally:
                # rate-limit between requests, but not after the last one
                if i < len(urls):
                    time.sleep(delay)

    # skipped log
    if skipped:
        lines = [f"{datetime.now(timezone.utc).isoformat()}  scrape run"]
        lines += [f"{u}\t{reason}" for u, reason in skipped]
        with skipped_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    print(f"{'─' * 70}")
    print(f"[summary] saved={saved}  skipped={len(skipped)}  total={len(urls)}")
    print(f"[summary] corpus dir: {out_dir}")
    if skipped:
        print(f"[summary] failures logged to: {skipped_path}")
    print(
        "[next] index the saved files via the agent's existing tool, e.g.:\n"
        '       uv run agent7.py "Index every .md file under finance/ '
        'and confirm how many chunks were added."'
    )


if __name__ == "__main__":
    main()
