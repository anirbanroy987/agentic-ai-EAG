"""
Async client for the LLM Gateway V2.

Speaks the gateway's real ``POST /v1/chat`` contract (see
``llm_gatewayV2/main.py`` and ``llm_gatewayV2/schemas.py``):

  request : { prompt, system, cache_system, provider, reasoning,
              response_format:{type,schema,name,strict}, max_tokens, temperature }
  response: { provider, model, text, parsed, input_tokens, output_tokens,
              cache_read_input_tokens, latency_ms, attempted, ... }

The public surface (``GatewayClient.call`` → ``GatewayResponse`` with
``.parsed/.provider/.model/.latency_ms/.input_tokens/.output_tokens/
.cache_hit/.raw_text``) is unchanged so ``src/agents.py`` keeps working.

Resilience: the agents pin a provider per stage. The gateway does NOT fail
over when a provider is explicitly named (unknown key → 400, provider error
→ 502, prompt over that provider's context cap → 503). So if a pinned call
fails for an infrastructure reason, we transparently retry once *without*
the pin and let the gateway route through its normal order.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "http://localhost:8100")
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 4096

# 503 = "all providers unavailable" (every provider on cooldown / rate
# limited). With a single provider and parallel fan-out this is expected,
# not fatal — back off and retry rather than crashing the pipeline.
_TRANSIENT_MAX_RETRIES = 7
_TRANSIENT_BASE_SLEEP = 4.5  # Gemini free-tier cooldown is ~4s


class GatewayError(Exception):
    """Raised when the gateway itself fails or returns malformed data."""


class GatewayResponse(BaseModel):
    parsed: Any
    provider: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False
    raw_text: str = ""


class GatewayClient:
    def __init__(
        self,
        base_url: str = GATEWAY_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def call(
        self,
        *,
        system_prompt: str,
        user_message: str,
        response_schema: Type[T],
        preferred_provider: str | None = None,
        reasoning: str = "medium",
        cache_system_prompt: bool = True,
        max_retries: int = 2,
    ) -> GatewayResponse:
        """Structured-output call. Returns a parsed Pydantic instance."""
        schema = response_schema.model_json_schema()

        def build_payload(provider: str | None, msg: str) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "prompt": msg,
                "system": system_prompt,
                "cache_system": cache_system_prompt,
                "reasoning": reasoning,
                "max_tokens": DEFAULT_MAX_TOKENS,
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "schema": schema,
                    "name": "out",
                    # Pydantic v2 schemas (Optionals → anyOf/null, $defs, etc.)
                    # routinely trip OpenAI-compat "strict" json_schema mode.
                    # The gateway still does its own server-side jsonschema
                    # validation + one corrective retry, so relax strict here.
                    "strict": False,
                },
            }
            if provider:
                payload["provider"] = provider
            return payload

        last_validation_error: str | None = None
        pin = preferred_provider

        for attempt in range(max_retries + 1):
            msg = user_message
            if last_validation_error:
                msg = (
                    f"{user_message}\n\n"
                    f"Your previous response failed schema validation:\n"
                    f"{last_validation_error}\n"
                    f"Return JSON matching the schema exactly."
                )

            start = time.perf_counter()
            data, status, err = await self._acquire(build_payload, pin, msg)
            if data is None:
                raise GatewayError(
                    f"Gateway request failed (status={status}): {err}"
                )

            latency_ms = int((time.perf_counter() - start) * 1000)

            # Prefer the gateway's server-validated `parsed` dict; fall back
            # to re-parsing `text` ourselves.
            parsed_payload = data.get("parsed")
            raw_text = data.get("text", "") or ""
            try:
                if isinstance(parsed_payload, (dict, list)):
                    parsed_obj = response_schema.model_validate(parsed_payload)
                else:
                    parsed_obj = response_schema.model_validate_json(raw_text)
            except ValidationError as e:
                last_validation_error = str(e)
                if attempt == max_retries:
                    raise GatewayError(
                        f"Response failed schema validation after "
                        f"{max_retries} retries: {e}"
                    ) from e
                continue

            cache_read = int(data.get("cache_read_input_tokens", 0) or 0)
            return GatewayResponse(
                parsed=parsed_obj,
                provider=data.get("provider", "unknown"),
                model=data.get("model", "unknown"),
                latency_ms=latency_ms,
                input_tokens=int(data.get("input_tokens", 0) or 0),
                output_tokens=int(data.get("output_tokens", 0) or 0),
                cache_hit=cache_read > 0,
                raw_text=raw_text or (
                    str(parsed_payload) if parsed_payload is not None else ""
                ),
            )

        raise GatewayError("Exhausted retries")

    async def _acquire(
        self, build_payload, pin: str | None, msg: str
    ) -> tuple[dict | None, int | None, str | None]:
        """
        Get one successful gateway response, absorbing transient failures.

        - 400/502 (pinned provider unknown / errored): drop the pin and
          retry through the gateway's normal routing.
        - 503 (all providers on cooldown) or a connection error (gateway
          momentarily unreachable, e.g. restarting): back off and retry.
        - Anything else (e.g. 422 malformed request) is a real bug — surface
          it immediately rather than masking it behind retries.
        """
        use_pin = pin
        for i in range(_TRANSIENT_MAX_RETRIES + 1):
            data, status, err = await self._post(build_payload(use_pin, msg))
            if data is not None:
                return data, status, err

            if status in (400, 502):
                use_pin = None  # route through the gateway instead
                await asyncio.sleep(0.5)
                continue
            if status == 503 or status is None:
                use_pin = None
                if i >= _TRANSIENT_MAX_RETRIES:
                    return data, status, err
                await asyncio.sleep(_TRANSIENT_BASE_SLEEP + i * 1.5)
                continue
            # Non-transient (422/500/etc.) — don't paper over it.
            return data, status, err
        return None, status, err

    async def _post(
        self, payload: dict[str, Any]
    ) -> tuple[dict | None, int | None, str | None]:
        """POST /v1/chat. Returns (json, status, error). json is None on failure."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat", json=payload
                )
        except httpx.HTTPError as e:
            return None, None, f"HTTP error: {e}"

        if resp.status_code >= 400:
            body = ""
            try:
                body = resp.json().get("detail", "")
            except Exception:
                body = resp.text[:300]
            return None, resp.status_code, str(body)

        try:
            return resp.json(), resp.status_code, None
        except Exception as e:  # malformed body
            return None, resp.status_code, f"bad JSON body: {e}"
