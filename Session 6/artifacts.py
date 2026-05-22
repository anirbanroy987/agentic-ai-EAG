"""
ArtifactStore — in-RAM blob storage with simple integer IDs.

Per the Session 6 lecture:
  • IDs are plain integers ("1", "2", "3"...), NOT a hash. Weaker LLMs
    hallucinate long hex strings and invent their own IDs; short integers
    they can track reliably.
  • Bytes live in process RAM only — not persisted to disk by default.
    Persistence is an explicit architectural choice the caller can add
    (e.g. for GDPR retention) but is not the default.

Memory is the only thing that persists to JSON; this store does not.
"""
from __future__ import annotations

import time
from typing import Any


class ArtifactStore:
    """Process-local. Integer IDs. No disk."""

    def __init__(self):
        self._next_id: int = 1
        self._blobs: dict[str, bytes] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    # ── API used by the loop ───────────────────────────────────────────────

    def put_bytes(self, data: bytes, *, descriptor: str = "",
                  source: str = "") -> str:
        """Store `data`, return its short integer id as a string ("1","2",...).

        We return the id as a string because every other layer (Pydantic
        schemas, JSON over the wire to the LLM, the orchestrator's "attach="
        comparison) already treats it as one — pinning the *type* to str
        avoids needless coercion at every boundary. The integer-ness lives
        in the counter, not in the type.
        """
        art_id = str(self._next_id)
        self._next_id += 1
        self._blobs[art_id] = data
        self._meta[art_id] = {
            "descriptor": descriptor,
            "source": source,
            "size": len(data),
            "created_at": time.time(),
        }
        return art_id

    def exists(self, art_id: str) -> bool:
        return art_id in self._blobs

    def get_bytes(self, art_id: str) -> bytes:
        return self._blobs[art_id]

    def get_descriptor(self, art_id: str) -> str:
        return self._meta.get(art_id, {}).get("descriptor", "")

    def catalog(self) -> list[dict[str, Any]]:
        """Compact list of (id, descriptor, size) — what perception sees.
        Returned in insertion order (Python dict preserves it), so id "1"
        appears first."""
        return [
            {"id": k, "descriptor": v.get("descriptor", ""),
             "size": v.get("size", 0)}
            for k, v in self._meta.items()
        ]

    def reset(self) -> None:
        """Clear all artifacts (only the loop uses this between runs if it
        wants to)."""
        self._next_id = 1
        self._blobs.clear()
        self._meta.clear()


# Module-level singleton — the orchestrator imports this directly.
artifacts = ArtifactStore()
