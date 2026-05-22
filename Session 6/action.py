"""
Action — execute ONE tool call. No LLM here.

This is where the artifact wall gets crossed on the way OUT: when a tool
returns a payload bigger than ARTIFACT_THRESHOLD, we stash the full bytes
in the ArtifactStore and hand back a short descriptor instead. Perception
can later choose to materialize it back via `attach_artifact_id`.

Returns:
    (result_text, artifact_id)
        result_text  — short text safe to feed into the next decision turn
        artifact_id  — None when no artifact was needed; otherwise the id
                       perception can use to attach the full bytes later.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from mcp import ClientSession

from artifacts import artifacts as artifact_store
from schemas import ToolCall


# Anything longer than this becomes an artifact. Chosen so a Wikipedia
# fetch_url (~50-100k chars) always crosses, but a get_time / list_dir reply
# stays inline.
ARTIFACT_THRESHOLD = 4_000

# Truncated head we echo back into the conversation when we DID stash an
# artifact. Gives the decision LLM enough to recognize "yes that's the page
# I asked for" without burning context.
INLINE_HEAD = 1_200

# Per-tool wall-clock cap. crawl4ai's first run downloads a headless Chromium
# (~120 MB) and can take a minute or two; web_search via DDG can also drag.
# Without this the orchestrator hangs silently — there's no other timeout
# anywhere in the MCP stdio path.
TOOL_TIMEOUT_SECONDS = 180.0


def _extract_text(result) -> str:
    """MCP results can carry multiple content blocks; concatenate any text
    blocks. crawl4ai-style large returns will all sit in content[0].text."""
    if not result or not getattr(result, "content", None):
        return ""
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


async def execute(session: ClientSession, tool_call: ToolCall) -> tuple[str, Optional[str]]:
    """Run one MCP tool. Return (short_result_text, artifact_id_or_None)."""
    try:
        result = await asyncio.wait_for(
            session.call_tool(tool_call.name, tool_call.arguments or {}),
            timeout=TOOL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return (
            f"[tool_timeout] {tool_call.name} exceeded {TOOL_TIMEOUT_SECONDS:.0f}s. "
            f"If this is fetch_url on first run, crawl4ai may still be downloading "
            f"a headless Chromium — pre-install with `python -m playwright install "
            f"chromium`, then retry.",
            None,
        )
    except Exception as e:
        # Surface the error as the result text so the next decision turn can
        # react (e.g. choose a different tool or argument). No artifact.
        return f"[tool_error] {tool_call.name}: {e}", None

    text = _extract_text(result)

    if len(text) <= ARTIFACT_THRESHOLD:
        return text, None

    # Big payload — stash full bytes, hand decision a short head + the id.
    descriptor = _summary_descriptor(tool_call, text)
    art_id = artifact_store.put_bytes(
        text.encode("utf-8", errors="replace"),
        descriptor=descriptor,
        source=tool_call.name,
    )
    inline = (
        text[:INLINE_HEAD]
        + f"\n\n[stashed as artifact {art_id} — {len(text)} chars total. "
        f"Ask perception to attach if you need the rest.]"
    )
    return inline, art_id


def _summary_descriptor(tc: ToolCall, text: str) -> str:
    """One-line descriptor we attach to the artifact for later catalogue
    display. Tries to fish out a URL-ish argument for readability."""
    url_arg = tc.arguments.get("url") or tc.arguments.get("query") or ""
    if url_arg:
        return f"{tc.name}({url_arg}) — {len(text)} chars"
    args_str = json.dumps(tc.arguments, default=str)[:100]
    return f"{tc.name}({args_str}) — {len(text)} chars"
