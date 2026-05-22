"""
Single source of truth for every boundary in the agent6 loop.

Each Pydantic model below is consumed by at least two modules — keep them
here so perception/decision/action/memory all agree on the shape.

The loop runs one Goal at a time. Perception decomposes the user query into
GoalList; Decision converts one Goal into one ToolCall (or an answer);
Action runs the tool and may stash big payloads in the ArtifactStore;
Memory records the outcome with the artifact handle for later recall.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Gateway envelope (mirrors llm_gatewayV3/schemas.py ToolDef) ──────────────

class ToolDef(BaseModel):
    """What the V3 gateway expects on the request `tools=[...]` field."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ── Perception output ────────────────────────────────────────────────────────

def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


class Goal(BaseModel):
    """One unit of work the decision stage will tackle alone.

    `attach_artifact_ids` is perception's way of asking the loop to
    materialize stored bytes back into the next decision call — the only
    place that crosses the artifact wall. A synthesis goal ("recommend X
    given A and B") typically needs both A and B attached, hence the list.
    Empty list = no attachment.
    """
    id: str = Field(default_factory=_gen_id)
    text: str
    done: bool = False
    attach_artifact_ids: list[str] = Field(default_factory=list)
    note: str = ""


class GoalList(BaseModel):
    goals: list[Goal] = Field(default_factory=list)

    @property
    def all_done(self) -> bool:
        return bool(self.goals) and all(g.done for g in self.goals)

    def next_unfinished(self) -> Optional[Goal]:
        for g in self.goals:
            if not g.done:
                return g
        return None


# ── Decision output ──────────────────────────────────────────────────────────

class ToolCall(BaseModel):
    """The decision stage emits at most one of these per iteration."""
    id: str = Field(default_factory=_gen_id)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class DecisionOutput(BaseModel):
    """Either an answer or a tool call — never both. The orchestrator
    dispatches on `is_answer`."""
    is_answer: bool
    answer: Optional[str] = None
    tool_call: Optional[ToolCall] = None
    rationale: str = ""

    model_config = ConfigDict(extra="allow")


# ── Memory ───────────────────────────────────────────────────────────────────

MemoryKind = Literal[
    "fact", "preference", "scratchpad",
    "tool_outcome", "answer",
]


class MemoryItem(BaseModel):
    """One row in the memory store.

    `tool_outcome` rows are written by record_outcome() (no LLM call); the
    classifier-driven kinds (fact/preference/scratchpad) come from remember().
    """
    id: str = Field(default_factory=_gen_id)
    kind: MemoryKind = "fact"
    keywords: list[str] = Field(default_factory=list)
    descriptor: str
    value: dict[str, Any] = Field(default_factory=dict)
    artifact_id: Optional[str] = None
    run_id: Optional[str] = None
    goal_id: Optional[str] = None
    source: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
