"""
Entry point for the SchemeContext multi-agent advisor.

Usage:
    uv run main.py --query "I'm a 32-year-old farmer in Bihar, pincode 800001, 
                            family of 5, earning about 1.2 lakh per year"
    uv run main.py  # uses a default demo query
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

from src.orchestrator import generate_recommendation, render_markdown

# Windows terminals default to cp1252; Rich's Markdown renderer emits glyphs
# (e.g. ▌ U+258C) that codec can't encode, which would crash the program
# *after* all output files are already written. Force UTF-8 and bypass the
# legacy-Windows renderer so the run finishes cleanly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

load_dotenv()
console = Console(legacy_windows=False)


DEMO_QUERY = (
    "I'm a 32-year-old farmer in Bihar, pincode 800001. I have 1 acre of "
    "ancestral land. Family of 5 including 2 school-going kids. Annual "
    "income around 1.2 lakh. My wife wants to start a small dairy business. "
    "We live in a kachha 2-room house. No bank account problems but never "
    "applied for any government scheme."
)


def parse_args() -> tuple[str, str]:
    p = argparse.ArgumentParser(
        description="SchemeContext — multi-agent advisor for Indian government schemes."
    )
    p.add_argument(
        "--query",
        type=str,
        default=DEMO_QUERY,
        help="Free-text description of the user.",
    )
    p.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Where to write the recommendation and trace.",
    )
    args = p.parse_args()
    return args.query, args.output_dir


async def main() -> None:
    query, output_dir = parse_args()
    out_path = Path(output_dir)
    out_path.mkdir(exist_ok=True)

    console.print(
        f"[bold green]Generating recommendation for:[/]\n  {query}\n"
    )

    recommendation, verdict, trace = await generate_recommendation(query)

    # Write outputs.
    markdown = render_markdown(recommendation, query)
    md_path = out_path / "recommendation.md"
    md_path.write_text(markdown, encoding="utf-8")

    trace_path = out_path / "trace.json"
    trace_path.write_text(trace.model_dump_json(indent=2), encoding="utf-8")

    verdict_path = out_path / "verdict.json"
    verdict_path.write_text(verdict.model_dump_json(indent=2), encoding="utf-8")

    rec_path = out_path / "recommendation.json"
    rec_path.write_text(recommendation.model_dump_json(indent=2), encoding="utf-8")

    console.rule("[bold green]Final Recommendation")
    console.print(Markdown(markdown))

    console.rule("[bold cyan]Pipeline Summary")
    console.print(json.dumps(trace.summary(), indent=2))

    console.print(
        f"\n[bold]Saved:[/] {md_path}, {trace_path}, {verdict_path}, {rec_path}"
    )


if __name__ == "__main__":
    asyncio.run(main())
