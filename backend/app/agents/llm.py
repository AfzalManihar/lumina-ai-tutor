"""Centralised Groq client. Model is pinned to openai/gpt-oss-120b - there is no fallback model."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import REQUIRED_GROQ_MODEL, Settings
from app.core.errors import ConfigError, ProviderError, RateLimitError
from app.guardrails.output import parse_structured

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("lumina.llm")


@dataclass
class LLMResult:
    text: str
    usage: dict = field(default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})
    model: str = REQUIRED_GROQ_MODEL


class LLM(Protocol):
    model: str

    async def chat(self, messages: list[dict], *, max_tokens: int | None = None,
                   json_mode: bool = False, temperature: float = 0.2) -> LLMResult: ...


class TokenBudgetExceeded(ProviderError):
    code = "token_budget_exceeded"
    status_code = 402


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.model = settings.groq_model
        self._transport = transport
        if self.model != REQUIRED_GROQ_MODEL:  # defence in depth; Settings already enforces this
            raise ConfigError(f"Only {REQUIRED_GROQ_MODEL} is permitted")

    def _check(self) -> None:
        if not self.s.groq_api_key:
            raise ConfigError("GROQ_API_KEY is not configured on the server")

    async def chat(self, messages, *, max_tokens=None, json_mode=False, temperature=0.2) -> LLMResult:
        self._check()
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "temperature": temperature,
            "max_completion_tokens": max_tokens or self.s.llm_max_output_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.s.groq_api_key}"}
        last: Exception | None = None
        bumped = False
        for attempt in range(self.s.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.s.llm_timeout_seconds, transport=self._transport) as c:
                    r = await c.post(f"{self.s.groq_base_url}/chat/completions", json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = ProviderError("The language model provider timed out or is unreachable")
                await self._sleep(attempt, None)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = _retry_after(r)
                last = RateLimitError("The language model provider is rate limiting requests",
                                      details={"retry_after": retry_after or 5}) if r.status_code == 429 \
                    else ProviderError("The language model provider is temporarily unavailable")
                if attempt < self.s.max_retries:
                    await self._sleep(attempt, retry_after)
                continue
            if r.status_code == 400:
                err = _error_body(r)
                # Log the provider's reason (never the prompt or keys) so a 400 is diagnosable from server logs.
                logger.warning("LLM HTTP 400 code=%s message=%s", err.get("code"), str(err.get("message"))[:300])
                if err.get("code") == "json_validate_failed":
                    # gpt-oss spends part of max_completion_tokens on reasoning; a long JSON answer then gets cut off and
                    # the provider rejects it. Retry once with more room, then hand any partial text to the validator.
                    if not bumped:
                        bumped = True
                        payload["max_completion_tokens"] = min(int(payload["max_completion_tokens"]) * 2, 6000)
                        continue
                    if err.get("failed_generation"):
                        return LLMResult(text=str(err["failed_generation"]))
            if r.status_code in (401, 403):
                raise ConfigError("The language model provider rejected the server credentials")
            if r.status_code == 404 or (r.status_code == 400 and "model" in r.text.lower()):
                raise ProviderError(f"Model '{self.model}' is unavailable on the provider; no substitute is used")
            if r.status_code >= 400:
                raise ProviderError(f"The language model provider returned an error (HTTP {r.status_code})")
            return self._parse(r.json())
        assert last is not None
        raise last

    async def _sleep(self, attempt: int, retry_after: float | None) -> None:
        if attempt >= self.s.max_retries:
            return
        delay = min(retry_after if retry_after else (0.5 * 2 ** attempt + random.random() * 0.25), 8.0)
        await asyncio.sleep(delay)

    def _parse(self, data: dict) -> LLMResult:
        try:
            choice = data["choices"][0]
            text = (choice.get("message") or {}).get("content") or ""  # reasoning traces are never read or surfaced
            u = data.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Malformed response from the language model provider")
        if not text.strip():
            why = choice.get("finish_reason")
            raise ProviderError("The model returned no content" + (" (output limit reached)" if why == "length" else ""))
        usage = {"prompt": int(u.get("prompt_tokens", 0)), "completion": int(u.get("completion_tokens", 0)),
                 "total": int(u.get("total_tokens", 0)) or int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))}
        return LLMResult(text=text, usage=usage, model=data.get("model") or self.model)


def _error_body(r: httpx.Response) -> dict:
    try:
        e = r.json().get("error")
        return e if isinstance(e, dict) else {}
    except ValueError:
        return {}


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


class BudgetedLLM:
    """Wraps an LLM with a per-workflow token budget, call counter and usage accumulation."""

    def __init__(self, inner: LLM, budget_tokens: int, trace=None, max_repairs: int = 2):
        self.inner, self.budget, self.trace, self.max_repairs = inner, budget_tokens, trace, max_repairs
        self.used = 0
        self.calls = 0
        self.repairs = 0

    @property
    def remaining(self) -> int:
        return self.budget - self.used

    async def chat(self, messages, *, max_tokens=None, json_mode=False, temperature=0.2) -> LLMResult:
        if self.remaining <= 0:
            raise TokenBudgetExceeded("Workflow token budget exhausted")
        cap = min(max_tokens or 10**9, max(self.remaining, 1))
        res = await self.inner.chat(messages, max_tokens=cap if cap < 10**9 else None,
                                    json_mode=json_mode, temperature=temperature)
        self.calls += 1
        self.used += res.usage.get("total", 0) or max(1, len(res.text) // 4)
        if self.trace:
            self.trace.add_usage(res.usage)
        return res

    async def structured(self, messages: list[dict], model: type[T], *, max_tokens: int | None = None,
                         validate=None) -> T:
        """JSON-mode call validated against `model` (and an optional semantic `validate(obj)` that raises
        ValueError), with bounded repair attempts."""
        msgs = list(messages)
        err: str | None = None
        for attempt in range(self.max_repairs + 1):
            res = await self.chat(msgs, max_tokens=max_tokens, json_mode=True)
            try:
                obj = parse_structured(res.text, model)
                if validate is not None:
                    validate(obj)
                return obj
            except ValueError as e:
                err = str(e)
                self.repairs += 1
                if self.trace:
                    self.trace.retries += 1
                msgs = msgs + [{"role": "assistant", "content": res.text[:2000]},
                               {"role": "user", "content": f"Your previous output was invalid: {err}. "
                                                           "Reply again with ONLY corrected JSON matching the schema."}]
        raise ProviderError(f"Model output could not be validated after {self.max_repairs} repair attempts")
