"""The one place Claude is called from.

Every agent goes through ClaudeClient so that retries, refusal handling,
structured output and cost logging behave the same everywhere.

Dry-run: when ``dry_run`` is set (or no API key is configured and the caller
passes a ``stub``), the wrapper returns the caller's deterministic stub instead
of calling the API. Every agent supplies a stub, so the whole system runs
offline for demos and tests.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import anthropic
from pydantic import BaseModel
from sqlmodel import Session

from fullhouse.core.config import Settings
from fullhouse.core.models import ClaudeCall

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# USD per million tokens: (input, output, cache read). Cache writes are billed at 1.25x input.
PRICING: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"

RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


class ClaudeError(RuntimeError):
    pass


class ClaudeRefusal(ClaudeError):
    pass


def estimate_cost(model: str, usage) -> float:
    inp, out, cache_read = PRICING.get(model, PRICING["claude-opus-5-5"])
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        (usage.input_tokens or 0) * inp
        + (usage.output_tokens or 0) * out
        + cached * cache_read
        + cache_write * inp * 1.25
    ) / 1_000_000


class ClaudeClient:
    def __init__(
        self,
        settings: Settings,
        session: Session | None = None,
        dry_run: bool = False,
        client: anthropic.Anthropic | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.session = session
        self.dry_run = dry_run
        self._client = client
        self._sleep = sleep

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # Retries are ours (so they are logged); the SDK's own are switched off.
            self._client = anthropic.Anthropic(max_retries=0, timeout=300)
        return self._client

    # ------------------------------------------------------------------ public

    def text(
        self,
        system: str,
        prompt: str,
        *,
        agent: str,
        client_id: int | None = None,
        stub: Callable[[], str] | None = None,
        max_tokens: int = 4000,
    ) -> str:
        if self.dry_run:
            return self._stub(stub, agent)
        response = self._call(system, prompt, agent=agent, client_id=client_id, max_tokens=max_tokens)
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def structured(
        self,
        system: str,
        prompt: str,
        schema: type[T],
        *,
        agent: str,
        client_id: int | None = None,
        stub: Callable[[], T] | None = None,
        max_tokens: int = 8000,
    ) -> T:
        if self.dry_run:
            return self._stub(stub, agent)
        output_format = {"type": "json_schema", "schema": _strict_schema(schema)}
        response = self._call(
            system, prompt, agent=agent, client_id=client_id, max_tokens=max_tokens, output_format=output_format
        )
        raw = "".join(b.text for b in response.content if b.type == "text")
        try:
            return schema.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ClaudeError(f"{agent}: response did not match {schema.__name__}: {exc}") from exc

    # ---------------------------------------------------------------- internals

    def _stub(self, stub, agent):
        if stub is None:
            raise ClaudeError(f"{agent}: dry run requested but no stub supplied")
        log.info("%s: dry run, using stub instead of Claude", agent)
        return stub()

    def _call(self, system, prompt, *, agent, client_id, max_tokens, output_format=None):
        model = self.settings.claude_model
        output_config: dict = {"effort": self.settings.claude_effort}
        if output_format:
            output_config["format"] = output_format
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
        )
        if self.settings.claude_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
            create = self.client.beta.messages.create
        else:
            create = self.client.messages.create

        attempts = 0
        last_error: Exception | None = None
        while attempts <= self.settings.claude_max_retries:
            attempts += 1
            try:
                response = create(**kwargs)
            except RETRYABLE as exc:
                last_error = exc
                delay = _retry_delay(exc, attempts)
                log.warning("%s: Claude call failed (%s), retry %d in %.1fs", agent, type(exc).__name__, attempts, delay)
                self._sleep(delay)
                continue
            except anthropic.APIStatusError as exc:  # 4xx: not worth retrying
                self._log(agent, client_id, model, None, attempts, ok=False, error=f"{exc.status_code}: {exc.message}")
                raise ClaudeError(f"{agent}: Claude API error {exc.status_code}: {exc.message}") from exc

            self._log(agent, client_id, response.model or model, response.usage, attempts)
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise ClaudeRefusal(f"{agent}: Claude declined ({getattr(details, 'category', None)})")
            if response.stop_reason == "max_tokens":
                raise ClaudeError(f"{agent}: response hit max_tokens={max_tokens}")
            return response

        self._log(agent, client_id, model, None, attempts, ok=False, error=repr(last_error))
        raise ClaudeError(f"{agent}: Claude unavailable after {attempts} attempts: {last_error}")

    def _log(self, agent, client_id, model, usage, attempts, ok=True, error=None):
        row = ClaudeCall(agent=agent, client_id=client_id, model=model, attempts=attempts, ok=ok, error=error)
        if usage is not None:
            row.input_tokens = usage.input_tokens or 0
            row.output_tokens = usage.output_tokens or 0
            row.cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
            row.cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
            row.cost_usd = round(estimate_cost(model, usage), 6)
        log.info("%s: Claude %s in=%d out=%d $%.4f", agent, model, row.input_tokens, row.output_tokens, row.cost_usd)
        if self.session is not None:
            self.session.add(row)
            self.session.commit()


def _retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except ValueError:
                pass
    return min(2**attempt + random.random(), 60.0)


def _strict_schema(schema: type[BaseModel]) -> dict:
    """Pydantic JSON schema with additionalProperties=false on every object (required by structured outputs)."""
    js = schema.model_json_schema()

    def walk(node: dict) -> None:
        node.pop("title", None)
        node.pop("default", None)
        if "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
            for sub in node["properties"].values():
                walk(sub)
        if isinstance(node.get("items"), dict):
            walk(node["items"])
        for key in ("anyOf", "allOf", "oneOf"):
            for sub in node.get(key, []):
                walk(sub)
        for sub in node.get("$defs", {}).values():
            walk(sub)

    walk(js)
    return js
