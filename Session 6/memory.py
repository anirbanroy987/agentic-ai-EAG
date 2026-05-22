"""
Memory — persistent JSON store, keyword-overlap retrieval, one LLM call on
classifying user input (auto_route="memory"). Tool outcomes are recorded
without an LLM call — we already know the keywords (tool name + arg tokens).

Two write paths:
    • remember(text, source, run_id)
        Classify text via the gateway, persist fact/preference, drop
        scratchpad. Returns the stored MemoryItem (or None).
    • record_outcome(tool_call, result_text, artifact_id, run_id, goal_id)
        No LLM call. Builds keywords from the tool name + arg values + the
        result descriptor, persists as kind="tool_outcome" carrying the
        artifact handle so later recall can pull bytes back.

One read path:
    • read(query, history) → list[MemoryItem]
        Keyword overlap over (query ∪ recent history descriptors). Free.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _gateway import LLM
from schemas import MemoryItem, ToolCall


# Memory IS persisted to disk per the Session 6 lecture (artifacts are not).
# Default path is alongside the agent so a fresh checkout starts empty.
MEMORY_STORE: Path = Path(__file__).resolve().parent / "memory_store.json"


# ── Tokenization (shared with assignment.py) ─────────────────────────────────

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "my", "your", "what",
    "find", "to", "of", "in", "and", "or", "on", "at", "for", "with",
    "by", "from", "as", "s", "it", "this", "that", "be", "do", "did",
    "you", "i", "me", "we", "us", "they", "them", "tell",
}


def tokenize(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) > 1 and t not in STOPWORDS}


# ── Classifier (one LLM call, auto_route="memory") ───────────────────────────

MEMORY_SYSTEM = (
    "You are the memory classifier of an agentic loop. Read one piece of "
    "text and emit a MemoryItem JSON object with:\n"
    "  kind        — one of: fact | preference | scratchpad\n"
    "  keywords    — 3-8 lowercase content words, no stopwords\n"
    "  descriptor  — one-line summary (under 200 chars)\n"
    "  value       — dict of structured fields if extractable, else {}\n"
    "Use 'scratchpad' for transient working notes that should not be "
    "persisted. Be terse."
)


# ── Memory store ─────────────────────────────────────────────────────────────

class Memory:
    """JSON-backed list of MemoryItems. Loaded at agent start, saved on every
    write. The orchestrator imports the module-level `memory` singleton."""

    def __init__(self, path=MEMORY_STORE):
        self.path = path
        self.items: list[MemoryItem] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [MemoryItem.model_validate(r) for r in raw]
        except Exception:
            # Corrupt store — start fresh rather than crash the run.
            self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([m.model_dump(mode="json") for m in self.items], indent=2),
            encoding="utf-8",
        )

    # ── WRITE PATH 1 — classifier-driven (1 LLM call) ──────────────────────

    def remember(self, text: str, *, source: str = "", run_id: str = "") -> MemoryItem | None:
        """Classify + persist. Returns the MemoryItem when persisted; None for
        scratchpad (we never store transient working notes)."""
        # Trim the input we hand the classifier. Long Q+A pairs (an entire
        # final answer with markdown formatting) caused weaker free-tier
        # workers to exhaust max_tokens echoing the input back into
        # `descriptor` / `value`, returning truncated JSON which the gateway
        # then rejected as a 400. The classifier only needs enough text to
        # decide kind + extract keywords; it shouldn't be quoting the input.
        TRIM = 800
        classifier_input = text if len(text) <= TRIM else (
            text[:TRIM] + f"\n[...truncated; original was {len(text)} chars...]"
        )

        llm = LLM()
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["fact", "preference", "scratchpad"]},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "descriptor": {"type": "string"},
                "value": {"type": "object"},
            },
            "required": ["kind", "keywords", "descriptor", "value"],
        }
        reply = llm.chat(
            prompt=f"Classify this text:\n{classifier_input}",
            system=MEMORY_SYSTEM,
            # Lecture: cache flags are fake on free tiers — leave them off.
            response_format={
                "type": "json_schema", "schema": schema,
                "name": "MemoryClassification", "strict": True,
            },
            auto_route="memory",  # router can pick a cheap worker for this
            reasoning="off",
            # Lecture: temperature=1 with free providers.
            temperature=1,
            # Bumped from 384 — gives the model headroom to close a valid
            # JSON object even if it tries to be verbose in `descriptor`.
            max_tokens=1024,
        )

        parsed = reply.get("parsed")
        if not parsed:
            # Fallback — best-effort raw-text parse, then tokenize-on-the-fly.
            try:
                parsed = json.loads(reply.get("text") or "")
            except Exception:
                parsed = {
                    "kind": "fact",
                    "keywords": sorted(tokenize(text))[:8],
                    "descriptor": text[:200],
                    "value": {},
                }

        if parsed.get("kind") == "scratchpad":
            return None

        item = MemoryItem(
            kind=parsed.get("kind", "fact"),
            keywords=list(parsed.get("keywords") or []),
            descriptor=str(parsed.get("descriptor") or text[:200]),
            value=dict(parsed.get("value") or {}),
            source=source or None,
            run_id=run_id or None,
            created_at=datetime.now(timezone.utc).timestamp(),
        )
        self.items.append(item)
        self._save()
        return item

    # ── WRITE PATH 2 — tool outcome (no LLM call) ──────────────────────────

    def record_outcome(self, *, tool_call: ToolCall, result_text: str,
                       artifact_id: str | None, run_id: str = "",
                       goal_id: str = "") -> MemoryItem:
        """Persist a tool result row. Cheap — no classifier call."""
        kw = set()
        kw |= tokenize(tool_call.name)
        for v in tool_call.arguments.values():
            kw |= tokenize(str(v))
        kw |= tokenize(result_text)
        args_str = json.dumps(tool_call.arguments, default=str)[:120]
        descriptor = f"{tool_call.name}({args_str}) -> {result_text[:160]}"
        item = MemoryItem(
            kind="tool_outcome",
            keywords=sorted(kw)[:12],
            descriptor=descriptor,
            value={"tool": tool_call.name, "arguments": tool_call.arguments},
            artifact_id=artifact_id,
            run_id=run_id or None,
            goal_id=goal_id or None,
            source="tool",
            created_at=datetime.now(timezone.utc).timestamp(),
        )
        self.items.append(item)
        self._save()
        return item

    # ── READ PATH — keyword overlap (no LLM call) ──────────────────────────

    def read(self, query: str, history: list[dict[str, Any]] | None = None,
             top_k: int = 8) -> list[MemoryItem]:
        """Score items by overlap with the query AND recent history snippets,
        so this run's own outcomes can resurface mid-loop."""
        q = tokenize(query)
        for ev in (history or [])[-4:]:
            # Token-expand the query with descriptors from recent history.
            q |= tokenize(str(ev.get("result_descriptor") or ev.get("text") or ""))

        scored: list[tuple[int, float, MemoryItem]] = []
        for item in self.items:
            item_toks = set(item.keywords) | tokenize(item.descriptor)
            score = len(q & item_toks)
            if score:
                scored.append((score, item.created_at, item))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [m for _, _, m in scored[:top_k]]


# Module-level singleton — orchestrator imports this directly.
memory = Memory()
