"""Layer 2 — perception interpretation. The single biggest cost knob.

A raw `get_window_state` markdown tree is far too verbose to hand an LLM on
a real app (Calculator alone is ~9 KB, mostly menu-bar noise). This module
turns that markdown into the minimal slice the judge needs:

  1. PRE-FILTER at the source — the caller passes `query=` to get_window_state
     so the daemon trims the markdown (indices preserved; guide §6.4).
  2. REGEX-EXTRACT structured rows — `[element_index N] <Role> "Label"` —
     from whatever markdown comes back, OS-agnostically (handles both macOS
     `AXButton` roles and Windows UIA `Button`/`Edit` roles).
  3. FIRST-OCCURRENCE dedup — the macOS walker can emit a window subtree
     twice (guide §6.3); we keep the first index for each (role, label),
     which is the one anchored at the canonical window and most stable.

The knob: how aggressively to trim. `legend(limit=…)` caps rows handed to
the judge; `prioritise(subgoal)` floats query-relevant rows to the top so a
tight cap still keeps the actionable ones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .driver import WindowState

# Real cua-driver markdown (Windows v0.5.x), one addressable element per line:
#   - [7] Text "Display is 0" [id=CalculatorResults actions=[invoke]]
# i.e. a LEADING bracketed integer, then a role word, then a quoted label.
# `[id=…]` / `[value=…]` / `[actions=[…]]` brackets are never digit-only, so a
# digit-only bracket reliably marks the element index.
_ROW = re.compile(r'\[(\d+)\]\s+(\w+)\s+"([^"]*)"')
# Bare digit-only bracket (Windows) — first one on a line is the element index.
_IDX_BRACKET = re.compile(r"\[(\d+)\]")
# macOS doc format fallback: `… "label" [element_index 5]`.
_IDX_AX = re.compile(r"element_index\s+(\d+)", re.IGNORECASE)
# A quoted label anywhere on the line.
_LABEL = re.compile(r'"([^"]*)"')
# Role token: macOS AX* or common UIA/control role words.
_ROLE = re.compile(
    r"\b(AX[A-Za-z]+|Button|Edit|TextBox|StaticText|Text|MenuItem|MenuBar|"
    r"ListItem|CheckBox|RadioButton|ComboBox|TabItem|Hyperlink|Link|Image|"
    r"Slider|Group|Window|Document|Pane|ToolBar|TreeItem|Spinner|Table|Cell)\b"
)


@dataclass
class Row:
    index: int
    role: str
    label: str

    def render(self) -> str:
        return f'[{self.index}] <{self.role or "?"}> "{self.label}"'


@dataclass
class PerceptionView:
    rows: list[Row] = field(default_factory=list)
    raw_markdown: str = ""
    element_count: int = 0

    # ── lookups the cascade/judge use ───────────────────────────────────────
    def legend(self, limit: int = 80) -> str:
        if not self.rows:
            return "(no addressable elements)"
        shown = self.rows[:limit]
        more = f"\n… (+{len(self.rows) - limit} more)" if len(self.rows) > limit else ""
        return "\n".join(r.render() for r in shown) + more

    def find_label(self, text: str, *, exact: bool = False) -> Row | None:
        t = text.strip().lower()
        for r in self.rows:
            lab = r.label.strip().lower()
            if (lab == t) if exact else (t in lab):
                return r
        return None

    def by_role(self, role_substr: str) -> list[Row]:
        rs = role_substr.lower()
        return [r for r in self.rows if rs in (r.role or "").lower()]

    def read_text(self) -> list[str]:
        """Static/display text labels — the L1 'read a value' path. Returns
        non-empty text-row labels in tree order (e.g. a calculator display)."""
        out = []
        for r in self.rows:
            if "text" in (r.role or "").lower() and r.label.strip():
                out.append(r.label.strip())
        return out

    def prioritise(self, subgoal: str | None) -> "PerceptionView":
        """Stable-sort rows so those whose label appears in the subgoal float
        to the top — keeps a tight legend cap from dropping the row we need."""
        if not subgoal:
            return self
        terms = {w for w in re.findall(r"\w+", subgoal.lower()) if len(w) > 2}
        def score(r: Row) -> int:
            lab = r.label.lower()
            return -sum(1 for t in terms if t in lab)
        return PerceptionView(
            rows=sorted(self.rows, key=score),
            raw_markdown=self.raw_markdown, element_count=self.element_count,
        )


def parse_rows(markdown: str) -> list[Row]:
    """Extract one Row per markdown line that carries an element index.
    Handles the real Windows format (`[N] Role "Label" …`) and the macOS doc
    format (`… "Label" [element_index N]`). First-occurrence dedup on
    (role, label) per guide §6.3 (the walker can emit a subtree twice)."""
    rows: list[Row] = []
    seen: set[tuple[str, str]] = set()
    for line in markdown.splitlines():
        idx: int | None = None
        role = label = ""

        win = _ROW.search(line)
        if win:
            idx, role, label = int(win.group(1)), win.group(2), win.group(3)
        else:
            m = _IDX_BRACKET.search(line) or _IDX_AX.search(line)
            if not m:
                continue
            idx = int(m.group(1))
            role_m = _ROLE.search(line)
            role = role_m.group(1) if role_m else ""
            label_m = _LABEL.search(line)
            label = label_m.group(1) if label_m else ""

        key = (role, label)
        if label and key in seen:
            continue  # duplicate subtree — keep the first (canonical) index
        seen.add(key)
        rows.append(Row(index=idx, role=role, label=label))
    return rows


def filter(state: WindowState, subgoal: str | None = None) -> PerceptionView:
    """Turn a WindowState into the minimal interpretable view. Pre-filtering
    via get_window_state(query=…) happens at the call site; this does the
    structural extraction + dedup + (optional) subgoal prioritisation."""
    view = PerceptionView(
        rows=parse_rows(state.tree_markdown),
        raw_markdown=state.tree_markdown,
        element_count=state.element_count,
    )
    return view.prioritise(subgoal)
