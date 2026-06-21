# The `computer` skill — driving real desktop apps via cua-driver

Session 10 adds a `computer` skill to the Session 9 growing-graph agent. It
is the desktop twin of the Session 9 `browser` skill: a **cascade wrapper**
that owns a cheapest-layer-first escalation, routes every model call through
the **V9 gateway**, and plugs into the orchestrator through **one catalogue
entry + one dispatch branch**. Where `browser` drives web pages through
Playwright, `computer` drives native and Electron desktop apps through a
running **`cua-driver` daemon** (perception + action only).

cua-driver gives perception (AX/UIA tree scans, screenshots) and action
(click / type / hotkey / drag). Everything above it is this package.

---

## The five layers (and each one's cost knob)

| Layer | What it does | Cost knob | Module |
|------|--------------|-----------|--------|
| **1 — extract** | Read a value straight from the AX/UIA tree (no LLM) | n/a — free | `layers.extract_value` |
| **2a — deterministic** | Run a caller-supplied key/click sequence (no LLM) | sequence length | `layers.run_deterministic` |
| **2b — a11y judge** | Filter the tree → cheap text model picks ONE action/turn | *biggest knob:* how hard you trim the tree (`perception.filter`, `query=`) | `sequencing.run_subgoal` + `layers.judge` |
| **3 — vision** | Screenshot → grid set-of-marks → V9 vision → click (x,y) | escalation threshold (~10× L2b) | `vision.vision_fallback` |
| **page — Electron** | Drive Chromium apps via CDP CSS selectors | — | `electron.py` |

Plus **goal decomposition** (`metadata.subgoals`, or the goal itself) and
**error recovery** (`recovery.py`). The skill stops at the first layer that
satisfies the goal; `output.path` reports which one ran (surfaced in replay
exactly like `BrowserOutput.path`).

---

## The scan → act → verify loop and its two invariants

Every turn against a window (`sequencing.run_subgoal`):

```
SCAN    get_window_state(pid, win, query)   # Invariant A: scan before any indexed action
        ↳ empty? → recovery.handle_empty_tree (six traps)
DECIDE  perception.filter → layers.judge      # cheap text LLM → verdict
        ↳ verdict "escalate" → vision/page ; "done" → finish
ACT     driver.dispatch_action(...)           # by element_index, THIS scan only
VERIFY  get_window_state(...) again           # Invariant B: re-scan rebuilds the map
        ↳ post-condition unmet → recovery.on_verify_fail (fresh scan)
```

- **Invariant A** — a `get_window_state` precedes every element-indexed action that turn (the daemon cache is built by that scan).
- **Invariant B** — indices are **turn-scoped**; we re-scan after every state-changing action and never reuse an index. A cache-miss ("index N not found") is detected (`recovery.is_stale_index_error`) and resolved by re-scanning, not retrying.
- **Verify is mandatory** — `success: true` means the call *dispatched*, not that intent was met; we always re-scan and check a concrete post-condition.

---

## Six traps → six guards (`recovery.py`, platform-aware)

One symptom (`element_count == 0` / cache miss), six causes:

| Cause | Guard |
|------|-------|
| Permissions — macOS TCC / Windows UAC mismatch (probe Calculator → also 0 ⇒ global) | `PermissionsError` + OS-specific suspect list |
| Window backgrounded, not realised | activate (macOS AppleScript / **Windows `bring_to_front`**) → re-scan |
| Qt app on Linux without `QT_ACCESSIBILITY=1` | relaunch with the env var |
| UI reflowed → stale index (Invariant B) | re-scan, re-resolve |
| Electron app (one opaque WebArea) | relaunch with `electron_debugging_port`, drive via `page` |
| Game / canvas (no AX structure) | Layer 3 vision; no AX recovery possible |

The single highest-value guard is the empty-tree `PreconditionError`, which
lists every OS-relevant suspect instead of the bare cache-miss error.

---

## Cross-platform shim (`platform.py`)

The brief and `cua-driver.md` are macOS-centric; several rules **invert** on
Windows. All OS divergence lives in one module:

| Concern | macOS | Windows | Linux |
|--------|-------|---------|-------|
| binary path | `~/.local/bin/cua-driver` | `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin\cua-driver.exe` | `~/.local/bin/cua-driver` |
| daemon | `serve &` | `autostart enable` + `kick` | `serve &` |
| activate | AppleScript `activate` | **`bring_to_front`** | best-effort |
| modifier | `cmd` | `ctrl` | `ctrl` |
| launch identity | `bundle_id` | `name` | `name` |
| permission gate | TCC | none (UAC only if elevated) | Wayland portal |

`hotkey("mod", "z")` → `["cmd","z"]` on macOS, `["ctrl","z"]` elsewhere.

---

## The three tasks (shapes fixed; apps swappable)

1. **Zero-vision (L1 + L2a)** — *Calculator*: a deterministic click sequence computes a value; L1 reads the result from the tree. No LLM, no vision.
2. **Electron `page` path** — *VS Code / Slack*: relaunch with a debugging port, drive a DOM action via CDP selectors.
3. **Vision (L3)** — *MS Paint / a game*: act on a canvas via screenshot + grid set-of-marks + V9 vision → click/drag by (x,y).

L2b (a11y judge) is exercised by any AX-driven subgoal (e.g. a Notepad/Settings task), so all of L1 / L2a / L2b / L3 + escalation are exercised.

---

## Running it

Prerequisites: cua-driver installed + daemon up (see repo root), and the V9
gateway (auto-started by `gateway.ensure_gateway()` on `:8109`). L2b/L3 need a
text/vision provider configured in `llm_gatewayV9/agent_routing.yaml`.

**Predefined tasks (recommended — no shell-quoting pitfalls):**
```powershell
uv run python -m computer.tasks calc      # Task 1: Calculator 42x18 — zero LLM, zero vision (L2a+L1)
uv run python -m computer.tasks notepad   # L2b a11y judge (needs the V9 gateway)
uv run python -m computer.tasks paint     # Task 3: vision fallback (L3, needs gateway)
uv run python -m computer.tasks vscode    # Task 2: Electron page path (CDP)
uv run python -m computer.tasks list      # show all
```
> **PowerShell 5.1 caveat:** passing JSON on the command line (`run.py --sequence '[...]'`)
> gets mangled — PS strips the quotes around JSON keys. Use `computer.tasks`
> (metadata defined in Python) for anything with a sequence/subgoals. `run.py`
> is fine for flag-only runs (`--app`, `--goal`, `--force`).

**Standalone single run (`run.py`):**
```powershell
uv run python -m computer.run --app Calculator --goal "read the calculator display"
uv run python -m computer.run --app "Paint" --goal "draw a horizontal line" --force vision
```

**Through the orchestrator** (the two-edit integration):
```powershell
uv run python flow.py "Open Calculator and compute 42 times 18"
uv run python replay.py <session_id>     # step through; output.path shows the layer
```

**Offline logic tests (no binary needed):**
```powershell
uv run python -m computer.selftest
```

Every run is recorded to a trajectory dir (`recording.py`, stop in `finally`
so failures are captured too); `cua-driver replay_trajectory` reproduces it.

---

## Integration — Session 9 core untouched

- `agent_config.yaml` — one `computer:` catalogue entry (prompt + description, no provider pin).
- `skills.py` — one dispatch branch in `run_skill` (mirrors the `browser` branch): builds a `NodeSpec`, instantiates `ComputerSkill`, awaits `.run()`.
- `prompts/computer.md` — the skill prompt.
- `prompts/planner.md` — the Planner's skill list is static, so a new skill must be added there for `flow.py` to ROUTE to it (same as `browser` is described there). Without this the Planner falls back to `coder` for desktop tasks. This is a prompt edit, not orchestrator-logic change — `flow.py`/`recovery.py`/`schemas.py` stay untouched.
- The V9 client is **reused** (`_gateway.py` loads `browser/client.py` by path, dodging Playwright) — no new gateway, calls tagged `agent: computer` in the cost ledger.

Nothing else in Session 9 changes; no paid APIs; no external agent frameworks.

---

## Acceptance criteria → where they live

- 3 tasks, distinct L1/2a/2b/3 paths + escalation → `skill.py` cascade
- ≥1 vision, ≥1 Electron page, ≥1 zero-vision → tasks 3 / 2 / 1
- scan-act-verify + both invariants → `sequencing.py`
- empty-tree guard + six-trap recovery → `recovery.py`
- every run recorded → `recording.py`
- one catalogue entry + one dispatch line, core untouched, all calls via V9 → `agent_config.yaml`, `skills.py`, `_gateway.py`
