"""Recording & replay wrappers (guide §8, brief §10).

Every run is recorded into a turn-numbered trajectory directory: on failure
it is the evidence, on success it is a deterministic regression test
(`replay_trajectory` re-invokes every recorded call in order).

The context manager puts `stop_recording` in a finally block so a crashed or
failed run is still captured — the failed trajectory is the most useful one.
Start/stop are tolerant: a driver that can't record must not take down a real
task, but the failure is surfaced loudly because recording is required for
submission.
"""
from __future__ import annotations

from contextlib import contextmanager

from . import driver


def start(output_dir: str) -> dict:
    return driver.call("start_recording", {"output_dir": output_dir})


def stop() -> dict:
    return driver.call("stop_recording", {})


def get_state() -> dict:
    return driver.call("get_recording_state", {})


def replay(trajectory_dir: str) -> dict:
    """Re-run a recorded trajectory deterministically against the same UI."""
    return driver.call("replay_trajectory", {"trajectory_dir": trajectory_dir})


@contextmanager
def recording(output_dir: str):
    """`with recording(dir):` — start before the body, stop in finally so the
    trajectory captures failures too. Yields the output_dir for convenience."""
    started = False
    try:
        start(output_dir)
        started = True
    except driver.CuaError as e:  # noqa: BLE001
        print(f"[computer.recording] WARNING: could not start recording: {e}")
    try:
        yield output_dir
    finally:
        if started:
            try:
                stop()
            except driver.CuaError as e:  # noqa: BLE001
                print(f"[computer.recording] WARNING: stop_recording failed: {e}")
