"""Thin JSON-over-subprocess wrapper around the cua-driver tool surface.

Option 1 from CUA_DRIVER_GUIDE §3.3: shell out to
`cua-driver call <tool> <json>` through the running daemon (~30 ms/call).
No MCP adapter, no raw socket. The per-(pid, window_id) element_index
cache lives in the daemon, so every call here assumes
`daemon.ensure_daemon()` has already run once this session.

Tool/verb names match the guide exactly — note `type_text` (not `type`),
`hotkey {keys:[...]}`, and `get_window_state.capture_mode ∈ {som,ax,vision}`.
Argument values that differ by OS (launch identity, modifier keys) are
built through `platform.py`, never hard-coded here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from . import platform as plat


def _trace_enabled() -> bool:
    """Per-call so it can be toggled mid-session: set CUA_DEBUG=1 to print a
    `→ tool args` / `← result (ms)` line per cua-driver call to stderr. This is
    the cheapest way to track the flow of ANY stage (stderr keeps run.py's JSON
    stdout clean)."""
    return bool(os.environ.get("CUA_DEBUG"))


def _summarise(tool: str, d: dict) -> str:
    if not isinstance(d, dict):
        return repr(d)[:80]
    if tool == "get_window_state":
        return f"element_count={d.get('element_count')}"
    bits = [f"{k}={d[k]}" for k in ("pid", "window_id", "success") if k in d]
    return ", ".join(bits) or f"keys={list(d)[:6]}"


# ── exception hierarchy (imported by recovery.py) ─────────────────────────────
class CuaError(RuntimeError):
    """A cua-driver call returned non-zero or unparseable output."""


class CuaNotInstalled(CuaError):
    """The cua-driver binary could not be located on this machine."""


class PermissionsError(CuaError):
    """Empty tree for a *global* reason (TCC on macOS / UAC mismatch on
    Windows): the whole desktop is unreadable, not just one app. Probing a
    native app like Calculator and getting 0 too confirms this bucket."""


class PreconditionError(CuaError):
    """A turn precondition is unmet — almost always element_count == 0 for a
    per-app reason. Carries the OS-tuned suspect list so the message is
    actionable rather than the bare 'index N not found in cache'."""


# ── binary + raw call ─────────────────────────────────────────────────────────
def _bin() -> str:
    b = plat.resolve_binary()
    if not b:
        raise CuaNotInstalled(
            "cua-driver is not installed or not on PATH. Install it with:\n  "
            + plat.install_hint()
        )
    return b


def call(tool: str, args: dict[str, Any] | None = None, *, timeout: float = 60.0) -> dict[str, Any]:
    """Invoke one cua-driver tool through the daemon.

    Returns the parsed JSON object. Non-JSON stdout (some subcommands print
    plain text) comes back as {"raw": <stdout>}. Raises CuaError on a
    non-zero exit, CuaNotInstalled if the binary vanished mid-session.
    """
    debug = _trace_enabled()
    argv = [_bin(), "call", tool, json.dumps(args or {})]
    if debug:
        print(f"[cua] → {tool} {json.dumps(args or {})[:160]}", file=sys.stderr)
    t0 = time.time()
    try:
        # cua-driver emits UTF-8 (window text, JSON with non-ASCII). The Windows
        # default (cp1252) raises UnicodeDecodeError on those bytes and loses the
        # output, so decode UTF-8 explicitly with replacement.
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CuaError(f"{tool} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise CuaNotInstalled(
            "cua-driver binary disappeared. Reinstall:\n  " + plat.install_hint()
        ) from e
    dt = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        if debug:
            print(f"[cua] ✗ {tool} exit{proc.returncode} ({dt}ms): {msg[:140]}", file=sys.stderr)
        raise CuaError(f"{tool} failed (exit {proc.returncode}): {msg}")
    out = (proc.stdout or "").strip()
    if not out:
        d: dict = {}
    else:
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            d = {"raw": out}
    if debug:
        print(f"[cua] ← {tool} {_summarise(tool, d)} ({dt}ms)", file=sys.stderr)
    return d


def cli(*subcommand: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a non-`call` cua-driver subcommand (status, doctor, --version).
    Returns the raw CompletedProcess so the caller can inspect text output."""
    return subprocess.run([_bin(), *subcommand], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


# ── window state (the workhorse) ──────────────────────────────────────────────
@dataclass
class WindowState:
    pid: int
    window_id: int
    element_count: int
    tree_markdown: str
    capture_mode: str = "ax"
    raw: dict = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.element_count == 0


def get_window_state(pid: int, window_id: int, *, capture_mode: str = "ax",
                     query: str | None = None, timeout: float = 60.0) -> WindowState:
    """SCAN. Walks the app's AX/UIA tree for (pid, window_id) and rebuilds
    the daemon's element_index cache. `query` trims the rendered markdown
    (and only the markdown — indices are preserved; guide §6.4)."""
    args: dict[str, Any] = {"pid": pid, "window_id": window_id, "capture_mode": capture_mode}
    if query:
        args["query"] = query
    d = call("get_window_state", args, timeout=timeout)
    return WindowState(
        pid=pid, window_id=window_id,
        element_count=int(d.get("element_count") or 0),
        tree_markdown=d.get("tree_markdown") or "",
        capture_mode=capture_mode, raw=d,
    )


# ── discovery ─────────────────────────────────────────────────────────────────
def list_apps() -> dict[str, Any]:
    return call("list_apps", {})


def list_windows() -> dict[str, Any]:
    return call("list_windows", {})


def _window_id_of(entry: dict) -> int | None:
    wid = entry.get("window_id", entry.get("id"))
    return int(wid) if wid is not None else None


def windows_for(pid: int, *, source: dict | None = None) -> list[int]:
    """Window ids owned by `pid`. `source` lets us read the 'windows' field of a
    launch_app response directly (it carries windows too) before falling back
    to a list_windows call. Tolerant of window_id vs id key naming."""
    src = source if source is not None else list_windows()
    out: list[int] = []
    for w in (src.get("windows") or []):
        if "pid" in w and int(w.get("pid", -1)) != int(pid):
            continue
        wid = _window_id_of(w)
        if wid is not None:
            out.append(wid)
    return out


def first_window_id(pid: int) -> int | None:
    ws = windows_for(pid)
    return ws[0] if ws else None


def _candidate_windows() -> list[dict]:
    src = list_windows()
    return src.get("windows") or src.get("_legacy_windows") or []


def _bounds(w: dict) -> tuple[int, int, int, int]:
    b = w.get("bounds") or {}
    return (int(b.get("x", w.get("x", 0))), int(b.get("y", w.get("y", 0))),
            int(b.get("width", w.get("width", 0))), int(b.get("height", w.get("height", 0))))


def _is_junk_window(w: dict, *, strict: bool = True) -> bool:
    """Filter windows that are never the intended target: always the cua-driver
    agent-cursor overlay; in `strict` mode also windows parked offscreen or
    sized to a sliver (e.g. a minimised VS Code at x=-31993). Non-strict is for
    locating a *named* app that may legitimately be minimised."""
    title = (w.get("title") or "").lower()
    app_name = (w.get("app_name") or "").lower()
    if "agentcursoroverlay" in title or "cua-driver" in app_name:
        return True
    if not strict:
        return False
    x, y, width, height = _bounds(w)
    return x < -10000 or y < -10000 or width < 50 or height < 50


def find_app_windows(app: str, *, strict: bool = True) -> list[tuple[int, int]]:
    """All (pid, window_id) windows whose title or app_name matches `app`,
    largest first. Non-strict includes minimised/offscreen windows — used by the
    Electron read path, where the target app may be minimised and several
    instances may coexist."""
    target = (app or "").lower()
    matches = []
    for w in _candidate_windows():
        if _is_junk_window(w, strict=strict):
            continue
        if target and (target in (w.get("title") or "").lower()
                       or target in (w.get("app_name") or "").lower()):
            wid = _window_id_of(w)
            if wid is not None:
                matches.append((int(w.get("pid", -1)), wid, _bounds(w)[2] * _bounds(w)[3]))
    matches.sort(key=lambda m: -m[2])
    return [(p, w) for p, w, _ in matches]


def find_app_window(app: str, *, launch_pid: int | None = None,
                    retries: int = 12, delay: float = 0.4) -> tuple[int, int] | None:
    """Return (owner_pid, window_id) for the app's main window, or None.

    Crucial Windows detail: UWP apps (Calculator, Settings, …) are hosted by
    ApplicationFrameHost.exe, so the window's OWNER pid differs from the
    launch_app pid. We match by launch pid first (classic Win32 + macOS), then
    fall back to a title match (UWP) — and return the WINDOW's pid so
    get_window_state addresses the host that actually owns the tree. The
    window_id disambiguates which UWP app shares the host."""
    target = (app or "").lower()
    for _ in range(retries):
        cands = [w for w in _candidate_windows() if not _is_junk_window(w)]
        if launch_pid is not None:
            for w in cands:
                if int(w.get("pid", -1)) == int(launch_pid):
                    wid = _window_id_of(w)
                    if wid is not None:
                        return int(w["pid"]), wid
        if target:
            titled = [w for w in cands if target in (w.get("title") or "").lower()]
            if titled:
                titled.sort(key=lambda w: -(_bounds(w)[2] * _bounds(w)[3]))  # largest on-screen
                wid = _window_id_of(titled[0])
                if wid is not None:
                    return int(titled[0].get("pid", -1)), wid
        time.sleep(delay)
    return None


def launch_and_focus(app: str, *, electron_port: int | None = None,
                     retries: int = 12, delay: float = 0.4) -> tuple[int, int]:
    """Launch → find the real window → best-effort activate. Handles UWP host
    indirection (window owned by ApplicationFrameHost, not the launch pid) and
    both OS orderings. Returns (owner_pid, window_id) ready to scan."""
    launched = launch_app(app, electron_port=electron_port)
    lp = launched.get("pid")
    lp = int(lp) if lp is not None else None

    found = find_app_window(app, launch_pid=lp, retries=retries, delay=delay)
    if found is None and plat.OS_NAME == "macos" and lp is not None:
        plat.activate(pid=lp, app=app)             # realise a backgrounded window
        found = find_app_window(app, launch_pid=lp, retries=retries, delay=delay)
    if found is None:
        raise CuaError(f"no window found for {app!r} (launch pid={lp}); "
                       "check list_windows for the right title")
    pid, wid = found

    # Best-effort foreground. On Windows, foreground-lock can deny
    # SetForegroundWindow ("OS did not honor the swap") — but UIA usually reads
    # the tree without foreground, so a denied activation is NOT fatal. On macOS
    # the AX walk needs foreground, handled by the osascript activate above.
    try:
        plat.activate(pid=pid, app=app, window_id=wid)
    except CuaError as e:
        print(f"[driver] activation not honored (continuing — UIA may not need foreground): {e}")
    return pid, wid


# ── launch / lifecycle ────────────────────────────────────────────────────────
def launch_app(app: str, *, electron_port: int | None = None,
               additional_arguments: list[str] | None = None) -> dict[str, Any]:
    """Launch by the right identity key for this OS (name vs bundle_id).
    `electron_port` enables the CDP `page` path (macOS); `additional_arguments`
    are forwarded to the process (honored on Windows) — e.g. a file path so
    `code <path>` opens a named editor."""
    return call("launch_app", plat.launch_args(
        app, electron_port=electron_port, additional_arguments=additional_arguments))


def kill_app(pid: int) -> dict[str, Any]:
    return call("kill_app", {"pid": pid})


def bring_to_front(pid: int, *, window_id: int | None = None) -> dict[str, Any]:
    """Windows/Linux activation (needs window_id on Windows; succeeds when the
    cua-driver-uia UIAccess worker is present). On macOS this errors — use
    platform.activate (AppleScript) instead."""
    args: dict[str, Any] = {"pid": pid}
    if window_id is not None:
        args["window_id"] = window_id
    return call("bring_to_front", args)


# ── actions ───────────────────────────────────────────────────────────────────
def click(pid: int, window_id: int, *, element_index: int | None = None,
          x: int | None = None, y: int | None = None,
          modifier: list[str] | None = None, count: int | None = None) -> dict[str, Any]:
    """Click by element_index (semantic, preferred) XOR by (x, y) window-local
    pixels (vision path). Exactly one address mode must be given."""
    if (element_index is None) == (x is None or y is None):
        raise ValueError("click needs element_index OR (x, y), not both/neither")
    args: dict[str, Any] = {"pid": pid, "window_id": window_id}
    if element_index is not None:
        args["element_index"] = element_index
    else:
        args["x"], args["y"] = x, y
    if modifier:
        args["modifier"] = modifier
    if count:
        args["count"] = count
    return call("click", args)


def type_text(pid: int, window_id: int, text: str, *, dispatch: str | None = None) -> dict[str, Any]:
    """Insert text. dispatch='foreground' forces the real SendInput path —
    REQUIRED for Electron/Chromium content (VS Code editor), which silently
    drops the default background WM_CHAR posts. cua-driver does the brief
    foreground swap itself (deterministic; no user focus needed)."""
    args: dict[str, Any] = {"pid": pid, "window_id": window_id, "text": text}
    if dispatch:
        args["dispatch"] = dispatch
    return call("type_text", args)


def press_key(pid: int, window_id: int, key: str, *, dispatch: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"pid": pid, "window_id": window_id, "key": key}
    if dispatch:
        args["dispatch"] = dispatch
    return call("press_key", args)


def hotkey(keys: list[str], *, pid: int | None = None, window_id: int | None = None,
           dispatch: str | None = None) -> dict[str, Any]:
    """Key chord. Pass logical keys via platform.hotkey('mod','z') so the
    primary modifier is correct per OS. dispatch='foreground' reaches Electron
    content via SendInput."""
    args: dict[str, Any] = {"keys": keys}
    if pid is not None:
        args["pid"] = pid
    if window_id is not None:
        args["window_id"] = window_id
    if dispatch:
        args["dispatch"] = dispatch
    return call("hotkey", args)


def set_value(pid: int, window_id: int, element_index: int, value: str) -> dict[str, Any]:
    return call("set_value", {"pid": pid, "window_id": window_id,
                              "element_index": element_index, "value": value})


def page(pid: int, action: str, *, selector: str | None = None,
         window_id: int | None = None, **extra: Any) -> dict[str, Any]:
    """Electron/CDP DOM driving. Requires the app to have been launched with
    electron_debugging_port. Addresses by CSS selector, not element_index.
    On Windows the page tool also requires window_id (to pick the Electron
    window whose DevTools target to attach to)."""
    args: dict[str, Any] = {"pid": pid, "action": action}
    if window_id is not None:
        args["window_id"] = window_id
    if selector is not None:
        args["selector"] = selector
    args.update(extra)
    return call("page", args)


# ── normalized action dispatch (used by sequencing.py) ────────────────────────
def dispatch_action(pid: int, window_id: int, action: dict[str, Any]) -> str:
    """Execute one normalized action dict from the judge/verdict.

    Vocabulary: click(element_index) | type(element_index?,value) |
    key(value) | hotkey(keys) | set_value(element_index,value) |
    click_xy(x,y) | done | escalate. Returns "ok" or "error: …" — the loop
    treats any "error:" prefix as a failed turn, never a crash.
    """
    t = action.get("type", "")
    try:
        if t in ("done", "escalate"):
            return "ok"
        if t == "click":
            click(pid, window_id, element_index=int(action["element_index"]))
            return "ok"
        if t == "click_xy":
            click(pid, window_id, x=int(action["x"]), y=int(action["y"]))
            return "ok"
        if t == "type":
            ei = action.get("element_index")
            if ei is not None:
                click(pid, window_id, element_index=int(ei))
            type_text(pid, window_id, str(action.get("value", "")))
            return "ok"
        if t == "key":
            press_key(pid, window_id, str(action.get("value", "Return")))
            return "ok"
        if t == "hotkey":
            hotkey(list(action.get("keys", [])), pid=pid)
            return "ok"
        if t == "set_value":
            set_value(pid, window_id, int(action["element_index"]), str(action.get("value", "")))
            return "ok"
        return f"error: unknown action {t!r}"
    except CuaError as e:
        return f"error: {type(e).__name__}: {e}"
    except (KeyError, ValueError) as e:
        return f"error: bad action args for {t!r}: {e}"
