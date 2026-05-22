"""
Tiny shim so every PDA-M module can do `from _gateway import LLM`.

We deliberately DO NOT touch sys.path: the llm_gatewayV3/ folder contains
its own schemas.py, and inserting that folder on sys.path would shadow our
Session-6 schemas.py (Goal/GoalList/ToolCall would resolve to the gateway's
ChatRequest model instead). Load client.py by file path via importlib.

We also wrap LLM.chat to fold the gateway's JSON error body into the
exception message — httpx.raise_for_status() throws the body away and
that's exactly where the provider name and real reason live.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx

_CLIENT_PATH = Path(__file__).resolve().parent / "llm_gatewayV3" / "client.py"
_spec = importlib.util.spec_from_file_location("_v3_client", _CLIENT_PATH)
if _spec is None or _spec.loader is None:                          # pragma: no cover
    raise ImportError(f"could not load gateway client from {_CLIENT_PATH}")
_client_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_client_mod)
_RawLLM = _client_mod.LLM


class GatewayError(RuntimeError):
    """Carries the gateway's response body so callers can see *why* a 5xx
    happened (which provider, which schema validation, which upstream error).
    """
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"gateway {status} from {url}: {body}")
        self.status = status
        self.url = url
        self.body = body


class LLM(_RawLLM):
    """Drop-in subclass that surfaces gateway response bodies on HTTP errors
    AND turns connection-refused into a friendly one-liner so the user
    doesn't have to read a 60-line httpx traceback to learn that they
    forgot to start the gateway."""

    def chat(self, *args, **kwargs):
        try:
            return super().chat(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = e.response.text[:2000]
            except Exception:
                pass
            raise GatewayError(e.response.status_code, str(e.request.url), body) from e
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # The gateway isn't running (or not on the URL we're calling).
            # Status 0 is non-standard but unambiguous in our trace — it
            # could never be a real HTTP code, and it surfaces in
            # _should_fallback() as 'not recoverable' so the run dies fast
            # instead of hammering a dead socket through three providers.
            raise GatewayError(
                0, str(getattr(e, "request", None) and e.request.url or ""),
                f"gateway unreachable — is `python llm_gatewayV3/main.py` "
                f"running on the expected port? underlying: {e}"
            ) from e


__all__ = ["LLM", "GatewayError"]
