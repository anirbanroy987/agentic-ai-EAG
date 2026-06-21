"""Daemon lifecycle — bring `cua-driver serve` (the socket daemon) up once
per session so the element_index cache survives across `cua-driver call`
invocations.

Why this matters (guide §3.1/§3.2): in-process calls each spawn a fresh
process whose cache dies on exit, so a scan in one call and a click in the
next fails with "element index N not found in cache". The daemon holds the
cache in shared memory. Every action this package takes assumes the daemon
is up — hence ensure_daemon() at the top of ComputerSkill.run().

The start mechanism differs by OS (serve & vs autostart) and is described
entirely by platform.daemon_plan(); this module just executes it and polls
`cua-driver status` until ready.
"""
from __future__ import annotations

import subprocess
import time

from . import platform as plat
from .driver import CuaError, CuaNotInstalled, _bin, cli


def is_running() -> bool:
    """True when `cua-driver status` reports a live daemon. Tolerant of the
    exact wording (mac says 'is running', Windows status differs) — we treat
    a zero exit plus any 'run'/'listen'/'pid' marker as up."""
    try:
        proc = cli("status")
    except (CuaError, CuaNotInstalled, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    text = (proc.stdout + proc.stderr).lower()
    if proc.returncode != 0:
        return False
    return any(m in text for m in ("is running", "running", "listening", "pid", "socket"))


def ensure_daemon(*, wait_s: float = 20.0) -> None:
    """Idempotent: start the daemon if it is not already up, then block until
    `status` confirms it (or raise CuaError on timeout). Mirrors
    gateway.ensure_gateway()'s shape so the two prerequisites feel the same.
    """
    if is_running():
        return

    binary = _bin()  # raises CuaNotInstalled with an install hint if absent
    plan = plat.daemon_plan()
    for tail, background in plan.commands:
        argv = [binary, *tail]
        if background:
            # Long-running serve — detach and leave it; the poll below waits
            # for the socket. DEVNULL so it never inherits our pipes.
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Short-lived (Windows autostart enable/kick) — run to completion.
            subprocess.run(argv, capture_output=True, text=True, timeout=wait_s)

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_running():
            return
        time.sleep(0.5)
    raise CuaError(
        f"cua-driver daemon did not come up within {wait_s:.0f}s. "
        "Try manually: `cua-driver status`, then "
        + ("`cua-driver autostart kick`" if plat.OS_NAME == "windows" else "`cua-driver serve &`")
    )


def shutdown() -> None:
    """Best-effort daemon shutdown — a recovery/cleanup primitive (brief §11).
    Never raises: cleanup must not mask the original error in a finally block.
    """
    try:
        cli("shutdown")
    except Exception:  # noqa: BLE001 - cleanup is best-effort by contract
        pass
