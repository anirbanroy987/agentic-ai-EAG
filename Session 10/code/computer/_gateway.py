"""Reuse Session 9's V9 gateway client WITHOUT pulling in Playwright.

`browser/__init__.py` eagerly imports the DOM/driver modules (Playwright),
so a plain `from browser.client import V9Client` would force the desktop
skill to depend on a browser-automation stack it never uses. browser/client.py
itself is dependency-clean (httpx only, no relative imports), so we load it by
file path — the same importlib-by-path idiom gateway.py uses for the V9 LLM
client — and re-export V9Client. This is reuse, not a new gateway: it executes
the exact Session 9 client code, same /v1/chat + /v1/vision contract, same
`agent:` ledger tagging.
"""
from __future__ import annotations

import importlib.util as _ilu
import sys as _sys
from pathlib import Path

_CLIENT_PATH = Path(__file__).resolve().parent.parent / "browser" / "client.py"

if not _CLIENT_PATH.exists():  # pragma: no cover - layout guard
    raise ImportError(
        f"browser/client.py not found at {_CLIENT_PATH}. The computer skill "
        "reuses Session 9's V9 client; ensure the browser package is present."
    )

_spec = _ilu.spec_from_file_location("computer_v9_client", _CLIENT_PATH)
_mod = _ilu.module_from_spec(_spec)
# Register before exec: client.py uses `@dataclass` under `from __future__
# import annotations`, and dataclass processing resolves the class's module
# via sys.modules[cls.__module__] — which must exist or it raises.
_sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

V9Client = _mod.V9Client
GatewayResult = _mod.GatewayResult

__all__ = ["V9Client", "GatewayResult"]
