"""Model routing for the house brain: cheap tier first, the smart tier only when the cheap one is unsure.

Every call asks for JSON that matches a schema (structured outputs), so a job never parses prose. The price
table turns token counts into dollars for the run log; prices are USD per million tokens.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("hub.brain")

# USD per million tokens: (input, output, cache read, cache write 5m). Haiku 5.5 doubles past 100k prompt tokens.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
LONG_PROMPT = 100_000
LONG_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.50, 2.50, 0.05, 0.625),
}
UNKNOWN = PRICES["claude-sonnet-5-5"]  # an unlisted model is costed like Sonnet, so the log never under-reports


def price_of(model: str, prompt_tokens: int = 0) -> tuple[float, float, float, float]:
    base = re.sub(r"-\d{8}$", "", model or "")  # dated snapshots cost the same as their alias
    if prompt_tokens > LONG_PROMPT and base in LONG_PRICES:
        return LONG_PRICES[base]
    return PRICES.get(base, UNKNOWN)


def cost(model: str, tokens_in: int, tokens_out: int, cached: int = 0, cache_write: int = 0) -> float:
    """Dollars for one request. tokens_in is the uncached input; cached/cache_write are counted separately."""
    p_in, p_out, p_read, p_write = price_of(model, tokens_in + cached + cache_write)
    return round((tokens_in * p_in + tokens_out * p_out + cached * p_read + cache_write * p_write) / 1_000_000, 6)


@dataclass
class Completion:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached: int = 0
    cache_write: int = 0


class Provider(Protocol):
    async def complete(self, *, model: str, system: str, prompt: str, schema: dict[str, Any],
                       max_tokens: int, effort: str) -> Completion: ...


class NoKey(Exception):
    pass


class AnthropicProvider:
    """Claude through the same key as Ask Thomas Creek."""

    def __init__(self, hub: Any):
        self.hub = hub
        self._client: Any = None
        self._key: str | None = None

    def _anthropic(self) -> Any:
        key = self.hub.vault.get(self.hub.config.brain.key_secret)
        if not key:
            raise NoKey("no Claude API key on the hub")
        if self._client is None or self._key != str(key):
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=str(key), max_retries=2, timeout=45)
            self._key = str(key)
        return self._client

    async def complete(self, *, model: str, system: str, prompt: str, schema: dict[str, Any],
                       max_tokens: int, effort: str) -> Completion:
        client = self._anthropic()
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        text = "".join(getattr(b, "text", "") or "" for b in resp.content if getattr(b, "type", "") == "text")
        u = getattr(resp, "usage", None)
        return Completion(
            text=text, model=getattr(resp, "model", None) or model,
            tokens_in=getattr(u, "input_tokens", 0) or 0, tokens_out=getattr(u, "output_tokens", 0) or 0,
            cached=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )


@dataclass
class Thought:
    """What one job got back from the model(s), with everything the run log needs."""

    data: dict[str, Any] | None
    model: str | None = None
    tier: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cached: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    escalated: bool = False
    error: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, c: Completion, tier: str, ms: int) -> None:
        self.model, self.tier = c.model, tier
        self.tokens_in += c.tokens_in + c.cache_write
        self.tokens_out += c.tokens_out
        self.cached += c.cached
        dollars = cost(c.model, c.tokens_in, c.tokens_out, c.cached, c.cache_write)
        self.cost_usd = round(self.cost_usd + dollars, 6)
        self.latency_ms += ms
        self.calls.append({"model": c.model, "tier": tier, "in": c.tokens_in, "out": c.tokens_out,
                           "cached": c.cached, "cost_usd": dollars, "ms": ms})


def parse_json(text: str) -> dict[str, Any] | None:
    """The model's JSON. Structured outputs make this exact; the fallback copes with a stray code fence."""
    text = (text or "").strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


class Router:
    """cheap -> (unsure) -> smart. A provider swap is all a test needs."""

    def __init__(self, hub: Any, provider: Provider | None = None):
        self.hub = hub
        self.provider: Provider = provider or AnthropicProvider(hub)

    @property
    def cfg(self) -> Any:
        return self.hub.config.brain

    def model_for(self, tier: str) -> str:
        return self.cfg.smart_model if tier == "smart" else self.cfg.cheap_model

    async def think(self, *, system: str, payload: Any, schema: dict[str, Any], tier: str = "cheap",
                    escalate: bool = True, max_tokens: int = 1200) -> Thought:
        prompt = payload if isinstance(payload, str) else json.dumps(payload, default=str, ensure_ascii=False)
        t = Thought(data=None)
        tiers = [tier] + (["smart"] if escalate and tier == "cheap" else [])
        for i, tr in enumerate(tiers):
            start = time.monotonic()
            try:
                c = await self.provider.complete(model=self.model_for(tr), system=system, prompt=prompt,
                                                 schema=schema, max_tokens=max_tokens, effort=self.cfg.effort)
            except NoKey as exc:
                t.error = str(exc)
                return t
            except Exception as exc:  # anthropic errors, network
                log.info("brain model call failed (%s): %s", tr, type(exc).__name__)
                t.error = f"{type(exc).__name__}"
                return t
            t.add(c, tr, int((time.monotonic() - start) * 1000))
            data = parse_json(c.text)
            if data is None:
                t.error = "model gave no JSON"
                continue
            t.data, t.error = data, None
            if not data.get("unsure") or i == len(tiers) - 1:
                break
            t.escalated = True
        return t
