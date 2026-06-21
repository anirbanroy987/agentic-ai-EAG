"""Layer 5 — vision fallback. The escape hatch for canvas/game/opaque apps
where the AX/UIA tree is empty or the target is inherently visual.

Pipeline (brief §4 / guide §7.3): cua-driver `capture_mode:"vision"`
screenshot → set-of-marks overlay → V9 `/v1/vision` → click by `(x, y)`.
This is ~10× the per-call cost of L2b, so the cascade only reaches it on an
`escalate` verdict or an unrecoverable empty tree.

Unlike the browser SoM driver, a desktop canvas exposes no discrete element
boxes, so the "marks" here are a coordinate grid that anchors the VLM's
pixel localisation. We reuse browser.highlight.to_data_url for encoding (and
could reuse annotate when element bounds are available).
"""
from __future__ import annotations

import asyncio
import base64
import tempfile
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from . import driver
from .layers import LayerResult


def to_data_url(png_bytes: bytes) -> str:
    """PNG bytes → data URL for the V9 /v1/vision `image` field. Inlined (it is
    two lines) so the computer package needs neither Playwright nor
    browser/__init__; identical to browser.highlight.to_data_url."""
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"

VISION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "action"],
    "properties": {
        "thinking": {"type": "string"},
        "action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "properties": {
                "type": {"type": "string",
                         "enum": ["click_xy", "type", "key", "drag", "done"]},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
                "value": {"type": "string"},
                "success": {"type": "boolean"},
                "note": {"type": "string"},
            },
        },
    },
}

SYSTEM_PROMPT_VISION = (
    "You drive a desktop app by looking at a screenshot of its window. A faint "
    "coordinate grid is overlaid with x/y pixel labels to help you localise. "
    "Coordinates are WINDOW-LOCAL pixels with origin at the window's top-left. "
    "Emit ONE action:\n"
    "  drag(x,y,to_x,to_y)     — press at (x,y) and DRAG to (to_x,to_y). THIS IS "
    "HOW YOU DRAW. Horizontal line = same y, different x, e.g. drag(500,400,800,400).\n"
    "  click_xy(x,y)           — click a pixel (buttons/tools only)\n"
    "  type(value)             — type text into the focused field\n"
    "  key(value)              — press a key like 'Return', 'Tab', 'Escape'\n"
    "  done(success,note)      — finish; success=true if the goal is met\n"
    "DRAWING TASKS: the large blank region is the CANVAS; the default "
    "pencil/brush is already selected, so just DRAG across the canvas to draw — "
    "do NOT click toolbar buttons or the title bar (top ~80px). After ONE drag "
    "that produces the requested shape, emit done(success=true).\n"
    "Be terse in `thinking`. Prefer one precise action."
)


def _overlay_grid(png: bytes, step: int = 100) -> bytes:
    """Draw a faint coordinate grid with axis labels — the desktop-canvas
    stand-in for set-of-marks, giving the VLM pixel anchors to localise to."""
    img = Image.open(BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    grid = (255, 0, 0, 70)
    for x in range(step, w, step):
        draw.line([(x, 0), (x, h)], fill=grid, width=1)
        draw.text((x + 2, 2), str(x), fill=(255, 0, 0))
    for y in range(step, h, step):
        draw.line([(0, y), (w, y)], fill=grid, width=1)
        draw.text((2, y + 2), str(y), fill=(255, 0, 0))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _downscale(png: bytes, maxw: int = 768) -> tuple[bytes, float]:
    """Shrink the screenshot to maxw width to speed up a local CPU VLM (fewer
    image tokens → much faster prefill; ~250s → ~60-100s for qwen2.5vl:3b).
    Returns (png, factor) where factor = new_w/orig_w; divide the VLM's coords
    by factor to map back to original window pixels for click/drag."""
    img = Image.open(BytesIO(png)).convert("RGB")
    w, h = img.size
    if w <= maxw:
        return png, 1.0
    factor = maxw / w
    img = img.resize((maxw, max(1, round(h * factor))), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), factor


def _capture(pid: int, window_id: int, artifacts_dir: str | None, turn: int) -> bytes:
    """Grab a window screenshot via capture_mode='vision' and return PNG bytes.
    cua-driver returns the PNG as base64 in `screenshot_png_b64` (there is NO
    screenshot_out_file param); we decode it and save a copy for the trajectory."""
    resp = driver.call("get_window_state", {"pid": pid, "window_id": window_id,
                                            "capture_mode": "vision"})
    b64 = resp.get("screenshot_png_b64") or ""
    if not b64:
        raise driver.CuaError(f"vision capture returned no screenshot (keys={list(resp)})")
    png = base64.b64decode(b64)
    if artifacts_dir:
        Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
        (Path(artifacts_dir) / f"turn_{turn:02d}_vision.png").write_bytes(png)
    return png


async def _vision_call(client, image_url: str, prompt: str, *,
                       provider: str | None, model: str | None, retries: int = 2):
    """Call V9 /v1/vision with retry + provider failover. The pinned provider
    (gemini) intermittently 503s on its free tier; rather than crash the whole
    run on one blip, retry it a couple times with backoff, then fall back to the
    gateway default (local ollama) so a cloud outage degrades to slow-but-works.
    Returns the VisionResult, or None if every attempt failed (caller turns that
    into a graceful LayerResult instead of an unhandled exception)."""
    attempts: list[tuple[str | None, str | None]] = [(provider, model)]
    if provider:                       # add one failover to the gateway default
        attempts.append((None, None))
    for prov, mod in attempts:
        for attempt in range(retries):
            try:
                return await client.vision(
                    image_url, prompt, system=SYSTEM_PROMPT_VISION,
                    schema=VISION_SCHEMA, schema_name="ComputerVisionAction",
                    max_tokens=512, provider=prov, model=mod,
                )
            except httpx.HTTPError:    # HTTPStatusError (503) + TimeoutException
                await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def vision_fallback(pid: int, window_id: int, goal: str, client, *,
                          app: str | None = None,
                          artifacts_dir: str | None = None,
                          provider: str | None = None,
                          model: str | None = None,
                          max_steps: int = 8) -> LayerResult:
    """Run the screenshot→VLM→act→verify loop. Returns a LayerResult with
    path='vision'. `provider`/`model` pin the V9 vision call (e.g. ollama +
    llama3.2-vision for a free local VLM). `app` lets us re-resolve the window
    each turn — UWP apps (Paint) recreate their window, invalidating window_id."""
    steps: list[dict] = []

    for turn in range(1, max_steps + 1):
        # UWP apps recreate windows mid-run → re-resolve + foreground each turn
        # so the screenshot and the click/drag target the current window.
        if app:
            cur = driver.find_app_windows(app, strict=False)
            if cur:
                pid, window_id = cur[0]
        try:
            driver.bring_to_front(pid, window_id=window_id)
        except driver.CuaError:
            pass
        try:
            png_small, factor = _downscale(_capture(pid, window_id, artifacts_dir, turn))
        except driver.CuaError as e:
            # UWP window died right at capture (handle went stale) — the next
            # turn's loop top re-resolves the window, so record and retry rather
            # than crash. Only give up if this was the final allowed turn.
            steps.append({"turn": turn, "action": {}, "outcome": f"error: capture failed: {e}"})
            if turn == max_steps:
                return LayerResult(False, "vision", note=f"capture failed (stale window): {e}",
                                   turns=turn, actions=steps)
            continue
        png = _overlay_grid(png_small)
        history = "\n".join(f"turn {s['turn']}: {s['action']} → {s['outcome']}"
                            for s in steps[-5:]) or "(none yet)"
        prompt = (f"GOAL: {goal}\n\nRECENT ACTIONS:\n{history}\n\n"
                  f"What is the next single action? Coordinates are window-local pixels.")
        result = await _vision_call(client, to_data_url(png), prompt,
                                    provider=provider, model=model)
        if result is None:
            steps.append({"turn": turn, "action": {},
                          "outcome": "error: vision call failed (503/timeout) after retries+failover"})
            return LayerResult(False, "vision",
                               note="vision provider unavailable (503/timeout) after retries + ollama failover",
                               turns=turn, actions=steps)
        parsed = result.parsed
        if not isinstance(parsed, dict) or "action" not in parsed:
            steps.append({"turn": turn, "action": {}, "outcome": "error: no parseable action"})
            return LayerResult(False, "vision", note="VLM produced no action",
                               turns=turn, actions=steps)

        a = parsed["action"]
        t = a.get("type")
        if t == "done":
            return LayerResult(bool(a.get("success", False)), "vision",
                               note=a.get("note", "vlm done"), turns=turn, actions=steps)

        try:
            if t == "click_xy":
                # VLM coords are in the DOWNSCALED image → map back to window px.
                driver.click(pid, window_id, x=round(int(a["x"]) / factor),
                             y=round(int(a["y"]) / factor))
            elif t == "type":
                driver.type_text(pid, window_id, str(a.get("value", "")), dispatch="auto")
            elif t == "key":
                driver.press_key(pid, window_id, str(a.get("value", "Return")))
            elif t == "drag":
                driver.call("drag", {"pid": pid, "window_id": window_id,
                                     "from_x": round(int(a["x"]) / factor),
                                     "from_y": round(int(a["y"]) / factor),
                                     "to_x": round(int(a["to_x"]) / factor),
                                     "to_y": round(int(a["to_y"]) / factor)})
            else:
                steps.append({"turn": turn, "action": a, "outcome": f"error: unknown {t!r}"})
                continue
            outcome = "ok"
        except (driver.CuaError, KeyError, ValueError) as e:
            outcome = f"error: {type(e).__name__}: {e}"
        steps.append({"turn": turn, "action": a, "outcome": outcome,
                      "thinking": parsed.get("thinking", "")})

    return LayerResult(False, "vision", note=f"step cap reached ({max_steps})",
                       turns=max_steps, actions=steps)
