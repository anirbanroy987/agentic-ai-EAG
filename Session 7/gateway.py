"""Bridge to llm_gatewayV7.

V7 is V3 plus a single new endpoint, `POST /v1/embed`. The session-version
mapping (V7 for Session 7) lets us evolve the gateway forward without
touching prior versions. V3 remains available for Session 6 agents.

Auto-starts the gateway on port 8107 if it is not already up, then
re-exports the V7 `LLM` client and a module-level `embed()` helper. Every
layer in this agent imports from here so the boot logic lives in one place.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

GATEWAY_V7_DIR = Path(__file__).resolve().parent / "llm_gatewayV7"
GATEWAY_URL = "http://localhost:8107"


def _is_up() -> bool:
    try:
        httpx.get(f"{GATEWAY_URL}/v1/routers", timeout=2.0)
        return True
    except Exception:
        return False


def ensure_gateway() -> None:
    """Start V7 if it is not already running. Idempotent."""
    if _is_up():
        return
    if not GATEWAY_V7_DIR.exists():
        raise RuntimeError(
            f"Gateway V7 directory not found at {GATEWAY_V7_DIR}. "
            "Build llm_gatewayV7 (Session 7 prerequisite) before running S7 code."
        )
    print(f"[gateway] launching llm_gatewayV7 from {GATEWAY_V7_DIR}")
    subprocess.Popen(
        ["uv", "run", "main.py"],
        cwd=str(GATEWAY_V7_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(45):
        time.sleep(1)
        if _is_up():
            print(f"[gateway] up on {GATEWAY_URL}")
            return
    raise RuntimeError(f"Gateway V7 failed to start within 45s. Check {GATEWAY_V7_DIR}")


# Load V7's client.py without polluting sys.path. The gateway dir has its
# own `schemas.py`, which would shadow ours if we put it on the path.
import importlib.util as _importlib_util

_client_path = GATEWAY_V7_DIR / "client.py"
if _client_path.exists():
    _spec = _importlib_util.spec_from_file_location("llm_gatewayV7_client", _client_path)
    _mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _BaseLLM = _mod.LLM

    # Transient-failure retry. The gateway 503s ("all providers unavailable")
    # when Gemini is momentarily quota-capped and the fallback chain hasn't
    # recovered yet; 502/429 and connection drops are the same class of blip.
    # A short backoff usually clears it. This lives in the gateway layer so
    # every role (perception / decision / memory) inherits resilience without
    # touching the agent loop or any prompt.
    _RETRY_STATUS = {429, 500, 502, 503, 504}
    # When the only live provider is rate-limited (e.g. Groq at 30 rpm with
    # Gemini/OpenRouter quota-exhausted), a 503 means "wait for the per-minute
    # window to reset". The later backoff steps deliberately exceed 60s so a
    # single rpm window fully clears before the final attempt gives up.
    _RETRY_ATTEMPTS = 6
    _RETRY_BACKOFF = (3, 8, 20, 45, 65)  # seconds between attempts

    def _retry(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(_RETRY_ATTEMPTS):
                try:
                    return fn(*args, **kwargs)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code not in _RETRY_STATUS:
                        raise
                    last_exc = e
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    last_exc = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    print(f"[gateway] transient error ({last_exc!r}); retry "
                          f"{attempt + 1}/{_RETRY_ATTEMPTS - 1} in {delay}s")
                    time.sleep(delay)
            raise last_exc

        return wrapper

    class _RetryLLM(_BaseLLM):
        """Drop-in LLM client that retries chat()/embed() on transient gateway errors."""

        @_retry
        def chat(self, *args, **kwargs):
            return super().chat(*args, **kwargs)

        @_retry
        def embed(self, *args, **kwargs):
            return super().embed(*args, **kwargs)

    LLM = _RetryLLM
else:
    LLM = None  # populated once V7 is built; importers should ensure_gateway() first


def embed(text: str, task_type: str = "retrieval_document") -> dict:
    """Compute an embedding for `text` via the gateway's V7 embed endpoint.

    Returns the full response dict: `{embedding, dim, model, provider,
    latency_ms, ...}`. The chosen embedding model is fixed at the gateway
    level. Changing it invalidates every FAISS index built against the old
    vectors, so callers should treat the model as a project-level constant.
    """
    ensure_gateway()
    if LLM is None:
        raise RuntimeError(
            "Gateway V7 client unavailable. Confirm llm_gatewayV7/client.py exists."
        )
    return LLM().embed(text, task_type=task_type)


__all__ = ["ensure_gateway", "LLM", "GATEWAY_URL", "GATEWAY_V7_DIR", "embed"]
