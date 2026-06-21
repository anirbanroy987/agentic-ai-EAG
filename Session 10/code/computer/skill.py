"""Session 10: the Computer skill — cascade wrapper around the cua-driver
layers, the desktop twin of Session 9's BrowserSkill.

Same shape as BrowserSkill: it translates the orchestrator's NodeSpec into a
typed output + AgentResult and owns the layer cascade, cheapest-first:

    Layer 1   — extract a value from the AX/UIA tree         (no LLM)
    Layer 2a  — deterministic key/click sequence            (no LLM)
    Layer 2b  — perception filter + V9 cheap-LLM judge       (text)
    Layer 3   — set-of-marks + V9 vision → click by (x,y)    (vision)
    page      — Electron/CDP DOM path for Chromium apps

Escalation: a layer escalates when the target is not addressable at that
layer (empty tree, judge `escalate`, inherently visual). The skill stops at
the first layer that satisfies the goal.

Every model call routes through the V9 gateway via browser.client.V9Client —
no new gateway, tagged `agent: computer` for the cost ledger. The whole run
is recorded; the trajectory dir is the submission evidence.
"""
from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field

from schemas import AgentResult, NodeSpec

from ._gateway import V9Client

from . import daemon, driver, electron, layers, perception, recovery, recording, vision
from . import platform as plat


_FILENAME_RE = re.compile(r"([A-Za-z0-9_./\\:-]+\.[A-Za-z0-9]{1,5})")
_CONTENT_RE = re.compile(
    r"(?:containing|contains|that says|saying|with the text|with content|"
    r"with the content|text:|:)\s*(.+)$",
    re.IGNORECASE,
)
_WRITE_VERBS = ("create", "write", "save", "make a file", "new file")


def safe_write_dir() -> Path:
    """A directory with NO spaces in its path. cua-driver forwards a file path
    via launch_app `additional_arguments` as a raw parameter string, so a path
    with spaces (e.g. '…\\Session 10\\code\\') gets split by `code` into garbage
    args. Prefer cwd if it's space-free, else home, else temp, else C:\\."""
    for cand in (Path.cwd(), Path.home(), Path(tempfile.gettempdir())):
        if " " not in str(cand):
            return cand
    return Path("C:/")


def decompose_write_goal(goal: str, *, default_dir: Path) -> tuple[list[dict], str, str] | None:
    """Goal-decomposition layer: turn a 'create/write <file> containing <text>'
    goal into the input-synthesis steps that perform it in a foreground editor.
    Returns (steps, absolute_path, content) so the caller can VERIFY the file
    on disk afterwards, or None when the goal isn't a file-write. The file is
    opened BY NAME (`code <path>`) so Ctrl+S saves with no Save As modal; a
    select-all before typing makes the write REPLACE existing content, so the
    result is exactly `content` whether or not the file already existed."""
    g = goal.lower()
    if not any(v in g for v in _WRITE_VERBS):
        return None
    fn = _FILENAME_RE.search(goal)
    if not fn:
        return None
    filename = fn.group(1)
    cm = _CONTENT_RE.search(goal)
    # Content comes from the goal; no goal-specific default. Empty if unstated
    # (a generic "create <file>" → empty file), so nothing is hardcoded.
    content = cm.group(1).strip().strip("'\"") if cm else ""
    p = Path(filename)
    path = filename if p.is_absolute() else str((default_dir / filename))
    # Open the file BY NAME first (`code <path>`), so the editor already has a
    # path. Then type + Ctrl+S saves to that known path with NO Save As modal
    # dialog — modal dialogs block cua-driver's synthesized input on Windows.
    # DETERMINISTIC (cua-driver's supported recipe): bring_to_front foregrounds
    # the window via the UIAccess worker, then type_text dispatch="auto" lands
    # WM_CHAR on the now-focused editor. No user clicking, no focus-wait.
    # (type_text dispatch="foreground" is NOT implemented in cua-driver.)
    steps = [
        {"action": "open_path", "path": path},                       # named editor, no dialog
        {"action": "wait", "seconds": 2.5},                          # let the tab open
        {"action": "bring_to_front"},                                # foreground VS Code (UIAccess)
        {"action": "wait", "seconds": 0.6},
        {"action": "hotkey", "keys": ["mod", "1"]},                  # cursor into editor group
        {"action": "wait", "seconds": 0.4},
        {"action": "hotkey", "keys": ["mod", "a"]},                  # select-all so type REPLACES, not appends (idempotent on a pre-existing file)
        {"action": "wait", "seconds": 0.3},
        {"action": "type_text", "text": content, "dispatch": "auto"},  # WM_CHAR to focused editor
        {"action": "wait", "seconds": 0.5},
        {"action": "hotkey", "keys": ["mod", "s"]},                  # save, no dialog
        {"action": "wait", "seconds": 1.0},
        {"action": "get_text"},
    ]
    return steps, path, content


class ComputerOutput(BaseModel):
    """Typed payload written into AgentResult.output. Local to this package so
    schemas.py (Session 9 core) stays untouched; `path` reports the cascade
    layer that actually ran, exactly like BrowserOutput."""
    app: str
    goal: str
    path: str  # extract | deterministic | a11y | vision | page
    turns: int = 0
    content: str | None = None
    actions: list[dict] = Field(default_factory=list)
    note: str = ""
    trajectory_dir: str | None = None


class ComputerSkill:
    NAME = "computer"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "computer",
                 judge_provider_pin: str | None = None,   # let the gateway route/failover
                 vision_provider_pin: str | None = "groqvision",  # Groq LPU (Llama 4 Scout) ~1-3s; gemini/ollama are failover
                 artifacts_root: str | None = None,
                 session: str | None = None,
                 max_steps: int = 12, max_steps_vision: int = 3):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.judge_provider_pin = judge_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.session = session
        self.max_steps = max_steps
        self.max_steps_vision = max_steps_vision

    # ── public entry point (mirrors BrowserSkill.run) ───────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        app = node.metadata.get("app") or (node.inputs[0] if node.inputs else "")
        goal = node.metadata.get("goal") or "interact with the app"
        if not app:
            return self._pack_error(app, goal, "no app given (metadata.app or inputs[0])")

        subgoals = node.metadata.get("subgoals") or [goal]
        force_path = node.metadata.get("force_path")          # a11y | vision | page
        sequence = node.metadata.get("sequence") or node.metadata.get("keys")
        expect = node.metadata.get("post_condition")
        port = node.metadata.get("debugging_port") or electron.DEFAULT_DEBUG_PORT
        want_electron = bool(node.metadata.get("electron")) or electron.is_electron(app)

        t0 = time.time()
        try:
            daemon.ensure_daemon()
        except driver.CuaError as e:
            return self._pack_error(app, goal, f"cua-driver unavailable: {e}",
                                    elapsed=time.time() - t0)

        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session,timeout=420.0)
        artifacts_dir = (str(self.artifacts_root / f"computer_{int(t0)}")
                         if self.artifacts_root else None)
        # Standalone runs (no artifacts_root) record under state/runs/ instead
        # of scattering cua_run_* dirs in the cwd, so evidence stays tidy.
        rec_dir = artifacts_dir or str(Path.cwd() / "state" / "runs" / f"run_{int(t0)}")
        Path(rec_dir).parent.mkdir(parents=True, exist_ok=True)

        with recording.recording(rec_dir):
            try:
                # ── Electron / page path (forces the CDP DOM route) ─────────
                # Always return the page result here — do NOT fall through to
                # the native AX cascade. The Electron subgoals are CDP step
                # dicts ({action,selector}), which the native cascade would
                # mishandle (it expects string subgoals).
                if force_path == "page" or want_electron:
                    eff_subgoals = subgoals
                    verify_path = node.metadata.get("verify_file")
                    verify_text = expect or ""
                    # No explicit steps + a write-style goal → decompose into
                    # input-synthesis steps (so a flow.py query with just
                    # app+goal can WRITE, not only read).
                    if eff_subgoals == [goal]:
                        # Use a space-free dir so the path can't be split when
                        # forwarded to `code` via additional_arguments.
                        gen = decompose_write_goal(goal, default_dir=safe_write_dir())
                        if gen:
                            eff_subgoals, verify_path, gen_content = gen
                            verify_text = verify_text or gen_content
                    res = await self._run_electron(app, eff_subgoals, port=port)
                    # Verify a write by the FILE ON DISK — get_text is not proof.
                    if verify_path:
                        res = self._verify_write(res, verify_path, verify_text)
                    return self._pack(res, app, goal, rec_dir, time.time() - t0)

                # ── Native cascade: launch → window → activate ─────────────
                pid, wid = self._launch_and_focus(app)

                # L2a: a caller-supplied deterministic sequence (zero LLM) ──
                if sequence:
                    res = layers.run_deterministic(pid, wid, sequence)
                    if res.success and (expect or layers.looks_like_read(goal)):
                        scan = driver.get_window_state(pid, wid, capture_mode="ax")
                        read = layers.extract_value(perception.filter(scan), goal)
                        res.content = read.content
                    return self._pack(res, app, goal, rec_dir, time.time() - t0)

                # Per-subgoal cascade (L1 read → L2b → escalate to L3) ──────
                last = layers.LayerResult(False, "a11y", note="no subgoals run")
                for sg in subgoals:
                    last = await self._run_subgoal_cascade(
                        pid, wid, sg, client, app, expect, force_path, artifacts_dir)
                    if not last.success:
                        break
                return self._pack(last, app, goal, rec_dir, time.time() - t0)

            except recovery.PermissionsError as e:
                return self._pack_error(app, goal, str(e), elapsed=time.time() - t0)
            except driver.CuaError as e:
                return self._pack_error(app, goal, str(e), elapsed=time.time() - t0)

    # ── launch helpers ──────────────────────────────────────────────────────
    def _launch_and_focus(self, app: str) -> tuple[int, int]:
        # Launch → resolve window → activate, handling both OS orderings and
        # the Windows "bring_to_front needs window_id" requirement. See
        # driver.launch_and_focus.
        return driver.launch_and_focus(app)

    # ── per-subgoal cascade ─────────────────────────────────────────────────
    async def _run_subgoal_cascade(self, pid, wid, subgoal, client, app, expect,
                                   force_path, artifacts_dir) -> "layers.LayerResult":
        # L1: read straight from the tree for read-only goals.
        if force_path is None and layers.looks_like_read(subgoal):
            scan = driver.get_window_state(pid, wid, capture_mode="ax")
            if not scan.empty:
                r = layers.extract_value(perception.filter(scan, subgoal), subgoal)
                if r.success:
                    return r

        # Canvas/drawing goals can't be performed via the AX tree (the judge
        # only sees the toolbar). Route them straight to L3 vision instead of
        # grinding the whole step budget clicking toolbar buttons. Generic:
        # fires for any draw/sketch/paint goal, from flow.py or run.py alike.
        if force_path is None and layers.looks_like_draw(subgoal):
            force_path = "vision"

        # Forced vision (smoke-test escape hatch, or a draw goal routed above).
        if force_path == "vision":
            return await vision.vision_fallback(
                pid, wid, subgoal, client, app=app, artifacts_dir=artifacts_dir,
                provider=self.vision_provider_pin, max_steps=self.max_steps_vision)

        # L2b: scan-act-verify with the judge.
        res = await sequencing.run_subgoal(
            pid, wid, subgoal, client, app=app, expect=expect,
            provider=self.judge_provider_pin, max_steps=self.max_steps)
        if not res.escalate:
            return res

        # Escalation: Electron app → page; otherwise → L3 vision.
        if electron.is_electron(app):
            return await self._run_electron(app, [subgoal], port=electron.DEFAULT_DEBUG_PORT)
        return await vision.vision_fallback(
            pid, wid, subgoal, client, app=app, artifacts_dir=artifacts_dir,
            provider=self.vision_provider_pin, max_steps=self.max_steps_vision)

    # ── Electron path ───────────────────────────────────────────────────────
    async def _run_electron(self, app, subgoals, *, port) -> "layers.LayerResult":
        """Drive an Electron app's DOM via the page tool. Subgoals here are
        CDP step dicts (metadata.subgoals = [{action, selector, value?}, …]) or
        plain strings used as a click selector.

        The page tool needs (pid, window_id) on Windows, so we resolve the
        window after launching with the debug port. NOTE: Electron apps are
        single-instance — if the target is already running WITHOUT the port,
        the relaunch focuses the existing instance and no CDP server is
        available; the page call then fails cleanly (we surface it, we do not
        kill the user's running app)."""
        # A structured step dict drives a specific page action; a plain-language
        # subgoal (what the Planner emits for a read) defaults to get_text —
        # NEVER treat a natural-language string as a CSS selector to click.
        steps = [(s if isinstance(s, dict) else {"action": "get_text"}) for s in subgoals]
        # CDP actions (execute_javascript/click_element) need a debug-port
        # relaunch; read actions (get_text/query_dom) work via UIA on the
        # already-open window, so we do NOT relaunch for those.
        cdp_actions = {"execute_javascript", "click_element"}
        read_actions = {"get_text", "query_dom"}
        needs_cdp = any(s.get("action") in cdp_actions for s in steps)

        if needs_cdp:
            launched = electron.relaunch_with_cdp(app, port=port)
            lp = launched.get("pid")
            cands = driver.find_app_windows(app, strict=False)
            cands.sort(key=lambda pw: 0 if (lp is not None and pw[0] == int(lp)) else 1)
        else:
            cands = driver.find_app_windows(app, strict=False)
            if not cands:
                driver.launch_app(app)
                for _ in range(12):          # cold-start apps (Notion) take a few seconds
                    time.sleep(1.0)
                    cands = driver.find_app_windows(app, strict=False)
                    if cands:
                        break
        if not cands:
            return layers.LayerResult(
                False, "page",
                note=f"no window for Electron app {app!r}; for CDP actions close it first "
                     "so a --remote-debugging-port relaunch can attach")

        # Step vocabulary: page actions (DOM/UIA) + input synthesis + wait.
        # Input (type_text/hotkey/key) goes to the FOREGROUND window via
        # SendInput, so a WRITE task must NOT retry across windows (it would
        # type repeatedly); only read-only tasks loop over candidate windows.
        page_actions = {"get_text", "query_dom", "click_element", "execute_javascript"}
        input_actions = {"hotkey", "type_text", "key", "wait", "open_path", "bring_to_front"}
        has_input = any(s.get("action") in input_actions for s in steps)
        windows_to_try = cands[:1] if has_input else cands[:4]

        last: layers.LayerResult | None = None
        for owner_pid, wid in windows_to_try:
            recorded: list[dict] = []
            captured: list[str] = []
            ok = True
            for i, step in enumerate(steps, start=1):
                a = step.get("action", "get_text")
                try:
                    if a in page_actions:
                        out = electron.page_action(
                            owner_pid, a, selector=step.get("selector"), window_id=wid,
                            **{k: v for k, v in step.items()
                               if k not in ("action", "selector")})
                        txt = out.get("text") or out.get("raw") or out.get("result")
                        if isinstance(txt, str) and txt.strip():
                            captured.append(txt.strip())
                        recorded.append({"step": i, "page": step, "outcome": "ok",
                                         "result_keys": list(out)})
                    elif a == "hotkey":
                        driver.hotkey(plat.hotkey(*step.get("keys", [])), pid=owner_pid,
                                      window_id=wid, dispatch=step.get("dispatch"))
                        recorded.append({"step": i, "page": step, "outcome": "ok"})
                    elif a == "type_text":
                        driver.type_text(owner_pid, wid, str(step.get("text", "")),
                                         dispatch=step.get("dispatch"))
                        recorded.append({"step": i, "page": step, "outcome": "ok"})
                    elif a == "key":
                        driver.press_key(owner_pid, wid, str(step.get("value", "Return")),
                                         dispatch=step.get("dispatch"))
                        recorded.append({"step": i, "page": step, "outcome": "ok"})
                    elif a == "wait":
                        time.sleep(float(step.get("seconds", 1.0)))
                        recorded.append({"step": i, "page": step, "outcome": "ok"})
                    elif a == "open_path":
                        # Open a named editor (`code <path>`) — no Save dialog later.
                        driver.launch_app(app, additional_arguments=[str(step.get("path", ""))])
                        recorded.append({"step": i, "page": step, "outcome": "ok"})
                    elif a == "bring_to_front":
                        # Deterministic foreground via the UIAccess worker so the
                        # subsequent type_text WM_CHAR lands. Non-fatal if denied.
                        try:
                            driver.bring_to_front(owner_pid, window_id=wid)
                            recorded.append({"step": i, "page": step, "outcome": "ok"})
                        except driver.CuaError as e:
                            recorded.append({"step": i, "page": step, "outcome": f"warn: {e}"})
                    else:
                        ok = False
                        recorded.append({"step": i, "page": step, "outcome": f"error: unknown action {a!r}"})
                        break
                except driver.CuaError as e:
                    ok = False
                    recorded.append({"step": i, "page": step, "outcome": f"error: {e}"})
                    break
            wants_content = any(s.get("action") in (read_actions | {"execute_javascript"})
                                for s in steps)
            if ok and (captured or not wants_content):
                return layers.LayerResult(
                    True, "page", note=f"page steps completed (pid={owner_pid}, window={wid})",
                    turns=len(steps), actions=recorded,
                    content=("\n".join(captured)[:4000] or None))
            last = layers.LayerResult(False, "page",
                                      note=f"steps incomplete / no content (pid={owner_pid})",
                                      turns=len(steps), actions=recorded)
        return last or layers.LayerResult(False, "page", note="page produced no result")

    # ── write verification (file on disk, not get_text) ────────────────────
    def _verify_write(self, res: "layers.LayerResult", path: str,
                      expected_text: str) -> "layers.LayerResult":
        """Override the layer's optimistic success with the truth: does the file
        exist on disk with the expected content? A `page get_text` returning
        window text is NOT proof a save happened (the editor may not have been
        focused, or the Save dialog may not have confirmed)."""
        p = Path(path)
        if not p.exists():
            res.success = False
            res.note = (f"FILE NOT SAVED at {path} — the editor was likely not focused "
                        "when keys were sent, or the Save dialog did not confirm. "
                        "(get_text is not proof of a save.)")
            return res
        body = p.read_text(errors="replace")
        if expected_text and expected_text.lower() not in body.lower():
            res.success = False
            res.note = f"file exists at {path} but does not contain {expected_text!r}"
            return res
        res.success = True
        res.note = f"verified: file saved at {path} ({len(body)} bytes)"
        res.content = body[:2000]
        return res

    # ── packers (mirror BrowserSkill) ───────────────────────────────────────
    def _pack(self, res, app, goal, rec_dir, elapsed) -> AgentResult:
        out = ComputerOutput(
            app=app, goal=goal, path=res.path, turns=res.turns,
            content=res.content, actions=res.actions, note=res.note,
            trajectory_dir=rec_dir,
        )
        return AgentResult(
            success=res.success, agent_name=self.NAME,
            output=out.model_dump(),
            error=None if res.success else res.note,
            error_code=None if res.success else "interaction_failed",
            elapsed_s=elapsed,
        )

    def _pack_error(self, app, goal, msg, *, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(app=app or "", goal=goal, path="extract", note=msg)
        return AgentResult(
            success=False, agent_name=self.NAME, output=out.model_dump(),
            error=msg, error_code="interaction_failed", elapsed_s=elapsed,
        )


# Imported late to avoid a heavy import at module load when only ComputerOutput
# is needed; sequencing pulls in the judge + perception chain.
from . import sequencing  # noqa: E402
