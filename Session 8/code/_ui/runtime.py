"""Subprocess management and on-disk session reading.

The dashboard never imports `flow.py` directly — that would bind the
Streamlit process's event loop to the orchestrator's asyncio loop and
the two don't share well. Instead we spawn `flow.py` as a subprocess
and treat `state/sessions/<sid>/` as the source of truth, exactly as
`replay.py` does.

Public surface:

    start_run(query, *, env_overrides) → subprocess.Popen
    stop_run(proc)                     → None
    extract_sid(line)                  → str | None
    load_nodes(sid)                    → list[dict]   (NodeState JSON)
    load_graph(sid)                    → dict | None  (raw graph.json)
    load_query(sid)                    → str
    list_sessions()                    → list[str]    (newest first)
    summarise_session(sid)             → dict         (status, n_done, etc.)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_ROOT = ROOT / "state" / "sessions"
FLOW_PY = ROOT / "flow.py"
SID_RE = re.compile(r"session\s+(s8-[a-z0-9]+)")
LOG_FILE = "run.log"  # per-session stdout transcript, written by the reader thread


# ── subprocess lifecycle ───────────────────────────────────────────────────

def start_run(query: str, *, env_overrides: dict[str, str] | None = None
              ) -> subprocess.Popen[str]:
    """Spawn `python flow.py "<query>"` and return the Popen handle.

    Stdout is utf-8 with errors='replace' — Windows cp1252 default would
    crash on the orchestrator's box-drawing header and on ₹ in finance
    answers. PYTHONIOENCODING is also exported into the child so its own
    print() calls encode safely.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, "-u", str(FLOW_PY), query],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
        encoding="utf-8", errors="replace",
    )


def stop_run(proc: subprocess.Popen[str] | None) -> None:
    """Terminate the subprocess (best effort). Idempotent."""
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass


def can_resume(sid: str) -> bool:
    """True if `flow.py --resume <sid>` would succeed.

    The Executor's resume path raises `RuntimeError("cannot resume ...:
    no graph.pkl on disk")` when `state/sessions/<sid>/graph.pkl` is
    missing — so checking for that file is the exact precondition.
    Sessions that errored out before the planner persisted the graph
    (rare) are correctly excluded.
    """
    if not sid:
        return False
    return (_session_dir(sid) / "graph.pkl").exists()


def start_resume(sid: str, *, env_overrides: dict[str, str] | None = None
                 ) -> subprocess.Popen[str]:
    """Spawn `python flow.py --resume <sid>` and return the Popen handle.

    Matches `start_run`'s subprocess setup verbatim (utf-8, unbuffered,
    same env-override surface) so the dashboard's reader thread and
    stdout-tee work identically for resumed runs. The orchestrator
    reads the persisted query from `state/sessions/<sid>/query.txt`
    on resume — we pass no query argument here.

    `flow.py` flips any `running` nodes back to `pending` on load and
    re-executes them, so a session killed mid-flight picks up where it
    left off. A session that completed cleanly will load, find no work,
    and exit immediately — safe, just wasted ~1 s.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, "-u", str(FLOW_PY), "--resume", sid],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
        encoding="utf-8", errors="replace",
    )


def is_running(proc: subprocess.Popen[str] | None) -> bool:
    return proc is not None and proc.poll() is None


# ── stdout reader (drained on a daemon thread) ─────────────────────────────

def spawn_reader(proc: subprocess.Popen[str], buf: deque,
                 sid_holder: dict[str, str | None]) -> threading.Thread:
    """Start a daemon that drains the child's stdout AND tees it to disk.

    Behavior:
      * Every line is appended to the in-memory deque (live tail for the
        Execution-logs tab) and to `state/sessions/<sid>/run.log` once
        the sid has been sniffed.
      * Lines that arrive BEFORE the sid line are held in `pending` and
        flushed to the log file the moment we open it — so the user sees
        the full transcript even for the orchestrator's pre-sid banner.
      * If the log file can't be opened (permission, disk full), we just
        keep using the deque — persistence is best-effort, never blocking.

    If this thread dies, the pipe buffer fills and flow.py blocks on its
    next print(). We wrap everything in try/except for that reason —
    drop a malformed line rather than freeze the orchestrator.
    """
    def _target() -> None:
        log_fh = None
        pending: list[str] = []  # lines seen before sid was known
        try:
            while True:
                try:
                    line = proc.stdout.readline() if proc.stdout else ""
                except (UnicodeDecodeError, ValueError) as e:
                    buf.append(f"[reader] decode skip: {e!r}")
                    continue
                if not line:
                    break
                stripped = line.rstrip()
                buf.append(stripped)

                # sid sniffing — once we know it, open the log file and
                # flush the pending pre-sid lines.
                if sid_holder.get("sid") is None:
                    m = SID_RE.search(line)
                    if m:
                        sid_holder["sid"] = m.group(1)
                        log_fh = _open_log(m.group(1))
                        if log_fh is not None:
                            for prev in pending:
                                try:
                                    log_fh.write(prev + "\n")
                                except OSError:
                                    pass
                            pending.clear()

                # Either tee to the open file, or buffer until sid arrives.
                if log_fh is not None:
                    try:
                        log_fh.write(stripped + "\n")
                        log_fh.flush()  # flush so the dashboard can read
                                        # the tail while the run is alive
                    except OSError:
                        # Disk gone bad — stop trying, keep the deque
                        try:
                            log_fh.close()
                        except OSError:
                            pass
                        log_fh = None
                elif sid_holder.get("sid") is None:
                    pending.append(stripped)
        except Exception as e:
            buf.append(f"[reader] fatal: {type(e).__name__}: {e}")
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass
            if log_fh is not None:
                try:
                    log_fh.close()
                except OSError:
                    pass

    th = threading.Thread(target=_target, daemon=True, name=f"flow-reader-{id(proc)}")
    th.start()
    return th


def _open_log(sid: str):
    """Open `state/sessions/<sid>/run.log` for appending; return None on failure.

    The session directory is created by flow.py before any stdout line that
    contains the sid, so by the time we get here it normally exists. We still
    call `mkdir(parents=True, exist_ok=True)` as a belt-and-braces in case
    the dashboard sees the sid before flow.py has materialized the dir
    (rare timing window).
    """
    try:
        d = _session_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        return open(d / LOG_FILE, "a", encoding="utf-8", errors="replace")
    except OSError:
        return None


def extract_sid(line: str) -> str | None:
    m = SID_RE.search(line)
    return m.group(1) if m else None


# ── on-disk session reading ────────────────────────────────────────────────

def _session_dir(sid: str) -> Path:
    return SESSIONS_ROOT / sid


def _read_text_tolerant(p: Path) -> str:
    """Read a text file written by older or newer versions of the orchestrator.

    Pre-fix sessions were written via cp1252 on Windows and contain bytes that
    aren't valid utf-8. Post-fix sessions are clean utf-8. The dashboard is a
    viewer, so it has to tolerate both — `errors="replace"` turns any
    un-decodable byte into U+FFFD instead of raising UnicodeDecodeError.
    A read that fails for any other reason (OS error) returns an empty string;
    the caller deals with the empty.
    """
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_nodes(sid: str) -> list[dict[str, Any]]:
    d = _session_dir(sid) / "nodes"
    if not d.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("n_*.json")):
        text = _read_text_tolerant(p)
        if not text:
            continue
        try:
            out.append(json.loads(text))
        except json.JSONDecodeError:
            # Either a partial write (process killed mid-rename) or a file the
            # replace-decoded into something json.loads doesn't accept. Skip
            # quietly — replay.py does the same.
            continue
    return out


def load_graph(sid: str) -> dict[str, Any] | None:
    p = _session_dir(sid) / "graph.json"
    if not p.exists():
        return None
    text = _read_text_tolerant(p)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def load_query(sid: str) -> str:
    p = _session_dir(sid) / "query.txt"
    if not p.exists():
        return ""
    return _read_text_tolerant(p)


def load_log(sid: str, tail: int = 800) -> list[str]:
    """Return up to the last `tail` stdout lines of a historical session.

    Pairs with `spawn_reader`'s tee: when the user clicks Load on a past
    session in the sidebar, we drop these lines into `ss.stdout_lines`
    so the Execution-logs tab renders the historical transcript rather
    than the empty deque of whatever the current process happens to be.

    Tail size matches the deque cap (800) in streamlit_app.py — if the
    file is bigger we keep only the tail; if it's smaller we return all
    of it. Empty list for a session that pre-dates this logging feature
    (the rest of its data still works, just no transcript).
    """
    p = _session_dir(sid) / LOG_FILE
    if not p.exists():
        return []
    text = _read_text_tolerant(p)
    if not text:
        return []
    lines = text.splitlines()
    return lines[-tail:] if len(lines) > tail else lines


def load_final_answer(sid: str) -> str:
    """Pull the formatter node's `final_answer` field from disk if present.

    Used by the sidebar history list to show a one-line preview of what
    each past session produced, so users can pick a session by its result
    rather than by sid alone.
    """
    try:
        for n in load_nodes(sid):
            if n.get("skill") == "formatter" and n.get("status") == "complete":
                out = (n.get("result") or {}).get("output") or {}
                ans = out.get("final_answer")
                if isinstance(ans, str):
                    return ans
    except Exception:  # noqa: BLE001
        pass
    return ""


def list_sessions(limit: int = 50) -> list[str]:
    """List session ids, newest first by directory mtime."""
    if not SESSIONS_ROOT.exists():
        return []
    items = [p for p in SESSIONS_ROOT.iterdir() if p.is_dir()]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in items[:limit]]


_EMPTY_SUMMARY = {
    "sid": "", "query": "(unreadable)", "status": "failed",
    "n_total": 0, "n_done": 0, "wall": 0.0, "mtime": 0.0,
}


def summarise_session(sid: str) -> dict[str, Any]:
    """Compact summary used by the history list.

    Wrapped in a broad try/except: the sidebar iterates over every session on
    disk, and one corrupt directory (a half-finished SIGKILL run, a session
    written by an older codebase version) must not take down the entire
    history list. The dashboard reports those as `failed` and moves on.
    """
    try:
        nodes = load_nodes(sid)
        q = load_query(sid)
        completed = [n for n in nodes if n.get("status") == "complete"]
        failed = [n for n in nodes if n.get("status") == "failed"]
        running = [n for n in nodes if n.get("status") == "running"]
        starts = [n["started_at"] for n in nodes if n.get("started_at")]
        ends = [
            n.get("completed_at") or n.get("started_at", 0)
            for n in nodes if n.get("started_at")
        ]
        wall = (max(ends) - min(starts)) if starts else 0.0
        status = (
            "running" if running else
            "failed" if failed else
            "complete" if completed and not running and not failed else
            "pending"
        )
        d = _session_dir(sid)
        return {
            "sid": sid,
            "query": q[:80] + ("…" if len(q) > 80 else ""),
            "status": status,
            "n_total": len(nodes),
            "n_done": len(completed),
            "wall": wall,
            "mtime": d.stat().st_mtime if d.exists() else 0.0,
        }
    except Exception:  # noqa: BLE001 — viewer must not crash on bad session
        return {**_EMPTY_SUMMARY, "sid": sid}


def compute_metrics(nodes: list[dict[str, Any]]) -> dict[str, float]:
    """End-to-end timing rollup.

    - wall_clock:        max(completed_at) − min(started_at) across all nodes
    - sum_elapsed:       Σ result.elapsed_s over completed nodes (the serial cost)
    - speedup:           sum_elapsed / wall_clock (1.0× = no fan-out)
    - in_flight:         count of running nodes (live)
    """
    import time as _time
    if not nodes:
        return {"wall_clock": 0.0, "sum_elapsed": 0.0, "speedup": 1.0,
                "in_flight": 0, "n_done": 0, "n_total": 0}
    starts = [n["started_at"] for n in nodes if n.get("started_at")]
    ends: list[float] = []
    for n in nodes:
        s = n.get("started_at")
        if s is None:
            continue
        e = n.get("completed_at")
        if e is None:
            e = _time.time() if n["status"] == "running" else s
        ends.append(e)
    wall = (max(ends) - min(starts)) if starts else 0.0
    sum_e = sum(
        (n.get("result") or {}).get("elapsed_s", 0.0) or 0.0
        for n in nodes if n["status"] == "complete"
    )
    speedup = (sum_e / wall) if wall > 0 else 1.0
    done = sum(1 for n in nodes if n["status"] == "complete")
    in_flight = sum(1 for n in nodes if n["status"] == "running")
    return {
        "wall_clock": wall, "sum_elapsed": sum_e, "speedup": speedup,
        "in_flight": in_flight, "n_done": done, "n_total": len(nodes),
    }
