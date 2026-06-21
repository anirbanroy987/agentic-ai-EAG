# Plan — Session 10 `computer` skill (cua-driver, cross-platform)

## Context

Session 9 added a `browser` skill: a cascade wrapper that owns its own
"cheapest-layer-that-works" escalation (HTML extract → deterministic →
a11y text → vision set-of-marks), routes every model call through the V9
gateway, and plugs into the growing-graph orchestrator via **one catalogue
entry + one dispatch branch**. Session 10 repeats that exact shape for a
new `computer` skill that drives **real desktop apps** through a running
`cua-driver` daemon (perception + action only). The skill supplies the
five layers the driver does not: goal decomposition, perception
interpretation, scan-act-verify sequencing, error recovery, and vision
fallback.

**Two facts that shape this build (verified, not assumed):**
1. `cua-driver` is **not installed** on this machine (not on PATH, no
   daemon). A prebuilt **Windows x86_64** binary is available (v0.5.5) with
   a PowerShell one-liner installer — Stage 0 is a real, non-blocking step.
2. The machine is **Windows 11**, but `cua-driver.md` and the brief are
   macOS-centric. Several "non-negotiable rules" invert on Windows. Per
   your choice, the skill targets a **cross-platform shim** so the same
   code runs on Windows and macOS.

**Decisions locked in (from clarifying Q&A):**
- Platform: **cross-platform shim** (Windows + macOS behind one module).
- Runtime: **real binary only** (no mock; Stage 0 installs cua-driver).
- Tasks: recommended **shapes** (zero-vision / Electron-page / vision);
  you will finalize the exact apps.

**Integration constraint (faithful to the brief):** Session 9 core stays
untouched. Existing-file edits are exactly **two** — `agent_config.yaml`
(catalogue entry) and `skills.py` (one dispatch branch). Everything else
is new files under `code/computer/` plus `code/prompts/computer.md`. We
**reuse** `browser/client.py::V9Client` (chat/vision/cost_by_agent) and
`browser/highlight.py` (annotate/to_data_url) rather than add a gateway or
duplicate code. No paid APIs; no schemas.py edit (reuse existing
`ErrorCode` literals + descriptive error strings).

---

## The Windows ⇄ macOS differences the shim must absorb

| Concern | macOS (guide/brief) | Windows (this machine) |
|---|---|---|
| Binary path | `~/.local/bin/cua-driver` | `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin\cua-driver.exe` (on PATH after install) |
| Daemon start | `cua-driver serve &` | `cua-driver autostart enable` + `cua-driver autostart kick` |
| Activate window (empty-tree trap) | `osascript … activate` | `bring_to_front` (Windows-only; **the brief's "do NOT use bring_to_front" is mac-only**) |
| Modifier key | `cmd` | `ctrl` (select-all `ctrl+a`, undo `ctrl+z`) |
| Launch identity | `bundle_id` | `name` |
| Empty-tree causes | TCC perms, background launch (`self_activation_suppressed`), Qt env (Linux) | UAC-elevated target, window not foreground, Electron, canvas (no TCC) |
| Permission gate | TCC (Accessibility + Screen Recording) | none for most apps |

Same 34 tools, same cache invariants, same JSON surface on both. Only the
six items above branch by OS — that is the entire job of `platform.py`.

**Tool names verified against `cua-driver.md`:** `get_window_state`
(`pid`,`window_id`,`capture_mode`∈{som,ax,vision},`query`; returns
`element_count`,`tree_markdown`), `list_apps`, `list_windows`,
`launch_app`, `kill_app`, `bring_to_front`, `click` (element_index | x,y),
`type_text`, `press_key`, `hotkey` (`keys:[...]`), `set_value`, `page`
(`pid`,`action`,`selector`; needs `electron_debugging_port` at launch),
`start_recording`/`stop_recording`/`replay_trajectory`, `start_session`/
`end_session`, agent-cursor tools. **Note:** action verb is `type_text`
(not `type`); undo is `hotkey {keys:[ctrl|cmd, z]}`.

---

## Module layout (`code/computer/`, mirrors `code/browser/`)

```
code/computer/
  __init__.py
  skill.py        # ComputerSkill.run(NodeSpec)->AgentResult — owns the cascade + packers (mirrors BrowserSkill)
  platform.py     # the shim: binary path, daemon cmds, activate primitive, modifier keys, launch identity, empty-tree cause table
  daemon.py       # ensure_daemon(), shutdown() — uses platform.py
  driver.py       # CuaError + call(tool, args) JSON-over-subprocess wrapper; get_window_state/dispatch helpers; empty-tree guard
  perception.py   # Layer 2: filter AX markdown -> minimal view (query pre-filter + regex rows; FIRST-occurrence dedup per guide §6.3)
  sequencing.py   # scan->act->verify loop; Invariant A (scan before indexed act) + B (re-scan after every act, indices turn-scoped); post_condition_met
  layers.py       # router L1 extract / L2a deterministic / L2b AX+cheap-LLM / L3 vision; verdict contract {verdict:"act"|"escalate", tool, element_index}
  recovery.py     # six-trap dispatcher (platform-aware) + PreconditionError(empty tree) + stale-index re-resolve + Electron relaunch + activation
  vision.py       # capture_mode=vision screenshot -> set-of-marks (reuse highlight.annotate when bounds exist) -> V9 /v1/vision -> click (x,y)
  electron.py     # KNOWN_ELECTRON_APPS set + relaunch with electron_debugging_port + page() CSS-selector driving (reuses S9 CDP know-how)
  recording.py    # start/stop/replay wrappers (try/finally so failed runs are captured)
  run.py          # safe standalone runner: daemon up + recording + try/finally + kill switches (kill_app, undo, shutdown)
```
Prompt lives at `code/prompts/computer.md` (repo convention; no provider pin).

---

## The five layers (each its own testable unit, with cost knob)

1. **Goal decomposition** — `metadata.goal` → ordered subgoals via
   `V9Client.chat` (cheap planner, grounded in `list_apps`). Knob: planner
   model. Skipped when `metadata.subgoals` is supplied.
2. **Perception interpretation** (`perception.py`, biggest knob) — never
   hand the LLM the full tree. `get_window_state(query=…)` to pre-filter,
   then regex-extract `[element_index N] <Role> "Label"` rows into a
   minimal view. Use the **first** occurrence of each label (guide §6.3).
3. **Action sequencing** (`sequencing.py`) — the scan→act→verify loop with
   Invariants A & B baked in; re-scan after every state-changing action.
4. **Error recovery** (`recovery.py`) — six-trap guards (table below),
   stale-index re-resolve, Electron relaunch, window activation, empty-tree
   `PreconditionError`.
5. **Vision fallback** (`vision.py`) — only on `escalate` verdict or empty
   tree: `capture_mode=vision` → set-of-marks → V9 vision → `click {x,y}`.
   ~10× L2b cost; gated.

**Verdict contract (the routing seam, mirrors Browser's `output.path`):**
`{"verdict":"act","tool":"click","element_index":5}` → dispatch by index;
`{"verdict":"escalate","reason":"…"}` → vision fallback.

---

## Core loop (sequencing.py)

```
turn(pid, win, subgoal):
  state = driver.get_window_state(pid, win, query=…)     # Inv A: scan first
  if state.element_count == 0: recovery.handle_empty_tree(...)  # guards then re-scan or raise
  view   = perception.filter(state, subgoal)             # biggest cost knob
  action = v9.judge(view, subgoal)                        # cheap chat -> verdict
  if action.verdict == "escalate": return vision.fallback(pid, win, subgoal)
  driver.dispatch(pid, action)                            # act by element_index (this scan only)
  after  = driver.get_window_state(pid, win)             # Inv B + verify (rebuilds map)
  if not post_condition_met(state, after, subgoal): return recovery.on_verify_fail(...)
  return after
```

## Six traps → six guards (recovery.py, platform-aware)

| Detected | Cause | Guard |
|---|---|---|
| 0 on first scan of ANY app (probe Calculator) | macOS TCC / Win UAC-elevated | raise `PermissionsError` + grant/elevation hint |
| 0 right after launch, window not foreground | background launch | macOS `osascript activate`; **Windows `bring_to_front`** → re-scan |
| 0 on Qt app, Linux | `QT_ACCESSIBILITY=1` unset | relaunch with env var |
| cache miss on a click that worked last turn | UI reflowed (Inv B) | re-scan, re-resolve index |
| 0 on known Electron app | one opaque AXWebArea | relaunch with `electron_debugging_port`, drive via `page` |
| 0 on game/canvas (Paint, Figma) | renderer paints pixels | L3 vision; no AX recovery |

Discriminator: permissions/UAC fail **globally** (Calculator also 0) vs
Electron/canvas fail **only on that app**. Single highest-value guard: the
empty-tree `PreconditionError` listing all suspects.

---

## Three tasks (shapes fixed; **apps you'll finalize**)

1. **Zero-vision (L2a)** — *Calculator*: compute e.g. 42×18 via
   deterministic clicks/hotkeys, read result from the AX tree. No LLM, no
   vision. → satisfies "≥1 task completes with zero vision calls."
2. **Electron page path** — *VS Code or Slack*: relaunch with
   `electron_debugging_port`, drive a DOM action via `page` (CSS selector).
   → satisfies "≥1 task uses the Electron page path."
3. **Vision (L3)** — *MS Paint or a game*: act on a canvas via set-of-marks
   + V9 vision → `(x,y)` clicks/drag. → satisfies "≥1 task uses vision."

L1 (read) and L2b (AX + cheap-LLM judge) are exercised by Task 1's result
read (L1) and a Notepad/Settings AX-action smoke test (L2b), so all of
L1/2a/2b/3 + escalation are visible in code per the rubric.

*You said you'll swap apps — these are defaults; tell me substitutions
(e.g. Notepad for Calculator, Discord for Slack) at Stage 8 or earlier.*

**Skill metadata contract** (mirrors Browser's url/goal): `app` (required),
`goal` (required), optional `subgoals`, `keys`/`sequence` (L2a),
`electron`/`debugging_port`, `force_path` ∈ {a11y,vision,page} escape hatch,
`post_condition` (verify hint).

---

## Session 9 integration (the two edits)

- **`code/agent_config.yaml`** — add a `computer:` entry shaped like
  `browser:` (prompt ref + description, no provider pin; temperature/
  max_tokens kept for registry uniformity, ignored by the dispatcher).
- **`code/skills.py`** — add one branch in `run_skill`, copied from the
  `browser` branch (build NodeSpec, instantiate `ComputerSkill` with
  `artifacts_root=state/sessions/<sid>/computer` + `session=sid`, await
  `.run(node_spec)`, return). The cost ledger then tags calls under
  `agent: computer`; `replay.py` surfaces the chosen layer via
  `output.path` exactly like Browser. Nothing else changes.

---

## Staged build order (pause after each for a live run)

0. **Install cua-driver (Windows).** `irm https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.ps1 | iex` → `cua-driver --version` → `cua-driver doctor` → `cua-driver autostart enable` + `cua-driver autostart kick` → `cua-driver status`.
1. **platform.py + daemon.py + driver.py.** `ensure_daemon`, `call(tool,args)`, `get_window_state`. Test: scan Calculator, print element_count + tree.
2. **Layers 1 + 2a.** Read a value; run deterministic hotkeys. Test: compute 42×18 with **zero LLM calls**, read result.
3. **sequencing.py + invariants.** Wrap actions in scan-act-verify with post-condition checks. Test: deliberately reuse a stale index, confirm the guard fires.
4. **Layer 2b.** perception filter + V9 chat judge + verdict dispatch on a Notepad/Settings task.
5. **recovery.py + electron.py.** Six-trap guards; relaunch + `page` on VS Code/Slack.
6. **Layer 3 vision.** Set-of-marks + V9 vision on Paint/game.
7. **recording.py + run.py safety.** start/stop in try/finally; kill switches (kill_app, ctrl/cmd-z, shutdown) — tested before recording.
8. **Catalogue + dispatch.** Add the yaml entry + the skills.py branch; run end-to-end via `flow.py`; step through with `replay.py`.

---

## Verification

- **Gateway up:** `gateway.ensure_gateway()` auto-starts V9 on :8109.
  Vision (Task 3) needs a vision-capable provider in
  `llm_gatewayV9/agent_routing.yaml` — watch the known Gemini 429 quota
  issue; pin/fallback to a non-exhausted vision provider.
- **Per-stage live tests** as listed above (each stage = one runnable check
  against a real app).
- **End-to-end:** drive each of the 3 tasks through `flow.py` with a
  `computer` node; confirm `output.path` reports the expected layer
  (deterministic / page / vision) and `cost/by_agent` attributes spend to
  `agent: computer`.
- **Recording is evidence:** every run wraps `start_recording`/
  `stop_recording` (finally-block captures failures too); save trajectory
  dirs. `replay_trajectory` reproduces a run; `replay.py` steps the graph.
- **Acceptance checklist** (brief §12): 3 tasks; ≥1 vision; ≥1 Electron
  page; ≥1 zero-vision; scan-act-verify + both invariants; empty-tree +
  six-trap recovery; runs recorded; one catalogue entry + one dispatch
  line; Session 9 core untouched; all calls via V9; no paid APIs.

## Safety (before any real run — brief §11)

Prefer a fresh OS user account; back up anything the agent can touch;
verify every destructive action before trusting it; wire + test kill
switches (`kill_app`, undo hotkey, `cua-driver` shutdown) first; never run
against real/important data.

## Explicit "do NOT" (baked into code review)

No `element_index` reuse across turns; no vision when AX suffices; no
action without a same-turn `get_window_state`; no trusting `success:true`
without a verify re-scan; `bring_to_front` only on Windows (AppleScript
activate on macOS); no Session 9 core edits beyond the two listed; no new
gateway; no paid APIs / external agent frameworks; no inventing tool names.
```