"""LLM provider protocol + the concrete clients.

Mirrors the DataClient pattern (hedge_fund/data/protocol.py): agents depend on the
`LLMClient` protocol, never a concrete provider. Any class with a
`complete(system, user) -> str` method plugs in — community providers welcome.

We deliberately do NOT use langchain's structured-output machinery: its
forced-tool mode breaks on Anthropic reasoning models (v1 carries the same
workaround). For chat providers, we ask for JSON in the prompt and parse it
ourselves; Jev adapts native typed answers into the same JSON contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import ClassVar, Protocol, runtime_checkable

import requests

from hedge_fund.llm import contract
from hedge_fund.llm.registry import (
    env_var_for,
    is_supported,
    provider_for,
    SUPPORTED_PROVIDERS,
)

DEFAULT_MODEL = "claude-opus-5-5"
# Korean mode (DATA_PROVIDER=kr) defaults to a free-tier Gemini Flash model.
# "-latest" aliases track Google's current Flash release, so the default does
# not go stale. Override with HEDGE_FUND_LLM_MODEL or --model
# (e.g. gemini-flash-lite-latest for higher free-tier limits).
KR_DEFAULT_MODEL = "gemini-flash-latest"

logger = logging.getLogger(__name__)

# Called with each piece of text as it arrives. None means don't stream.
TokenListener = Callable[[str], None] | None


class LLMParseError(ValueError):
    """The model's response did not contain parseable JSON."""


class LLMCallError(RuntimeError):
    """Provider failure with optional, credential-free persistence context."""

    def __init__(self, message: str, diagnostic_record: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostic_record = diagnostic_record


@runtime_checkable
class LLMClient(Protocol):
    """Protocol all LLM providers must satisfy.

    complete() returns text (native text or an adapter's compatibility JSON).
    Providers may optionally implement cache_key(agent, system, user) when
    their request contains additional semantics beyond the two prompts.
    Providers should raise on
    transport failure — the LLMAgent layer decides to abstain, not the
    provider.
    """

    model: str

    def complete(self, system: str, user: str) -> str: ...


class ChatLLM:
    """A langchain chat model behind the LLMClient protocol.

    Chat providers share this wrapper — only *constructing* the chat model
    differs, and that lives in make_llm(). Their response handling is written
    once; JevLLM handles TypeSafe's native typed responses separately.

    With an `on_token` listener the same call streams: the listener sees text
    as it arrives, and complete() still returns the whole response. Watching
    is a property of the client, not of the caller — so LLMAgent.predict(),
    the prompt cache, and the parse are untouched by it.
    """

    def __init__(self, model: str, chat, on_token: TokenListener = None,
                 rate_limit: RateLimitPolicy | None = None) -> None:
        self.model = model
        self._chat = chat
        self._on_token = on_token
        self._rate_limit = rate_limit

    def complete(self, system: str, user: str) -> str:
        if self._rate_limit is None:
            return self._complete(system, user)
        return self._rate_limit.call(lambda: self._complete(system, user), self.model)

    def _complete(self, system: str, user: str) -> str:
        messages = [("system", system), ("human", user)]
        if self._on_token is None:
            return _flatten(self._chat.invoke(messages).content)

        # Streaming chunks concatenate into the response, so they flatten
        # without a separator — the "\n" that joins whole-message blocks would
        # land mid-word here.
        parts: list[str] = []
        for chunk in self._chat.stream(messages):
            text = _flatten(chunk.content, sep="")
            if text:
                parts.append(text)
                self._on_token(text)
        return "".join(parts)


class RateLimitPolicy:
    """Free-tier guard: at most *max_concurrency* calls in flight (process
    wide, across threads) and exponential backoff on

    - 429 / RESOURCE_EXHAUSTED (quota): up to *max_retries* retries;
    - transient server or network failures — HTTP 500/502/503/504,
      DEADLINE_EXCEEDED ("Deadline expired"), UNAVAILABLE, timeouts — up to
      *transient_retries* retries.

    Delay before retry n (0-based, counted per kind) is
    min(max_delay, base_delay * 2**n) plus up to 1s of jitter, or the
    server's own "retry in Ns" hint when longer. Any other error (bad
    request, bad key …) is raised at once — the agent abstains, as before.

    A 429 whose server hint asks for more than *max_hint* seconds ("Please
    retry in 44m10s", retry_delay { seconds: 2650 }) is not retried: the
    agent abstains at once instead of sleeping through six backoffs. When it
    is the free tier's *daily* quota (quota_id …PerDay…), that is logged as
    such, and further calls to the same model fail fast — without spending
    a request — until the hinted time has passed.

    Tunable from the environment: LLM_MAX_CONCURRENCY (default 1),
    LLM_MAX_RETRIES (6), LLM_TRANSIENT_RETRIES (3), LLM_RETRY_BASE_SECONDS (5),
    LLM_RETRY_MAX_SECONDS (120), LLM_MAX_RETRY_HINT_SECONDS (60).
    """

    _semaphores: dict[int, threading.BoundedSemaphore] = {}
    _lock = threading.Lock()
    # model → monotonic time until which its daily quota is known spent (process wide)
    _exhausted: ClassVar[dict[str, float]] = {}

    def __init__(self, max_concurrency: int = 1, max_retries: int = 6, base_delay: float = 5.0,
                 max_delay: float = 120.0, sleep: Callable[[float], None] = time.sleep,
                 transient_retries: int = 3, max_hint: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self.max_retries = max(0, max_retries)
        self.transient_retries = max(0, transient_retries)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_hint = max_hint
        self._sleep = sleep
        self._clock = clock
        with self._lock:
            self._gate = self._semaphores.setdefault(
                self.max_concurrency, threading.BoundedSemaphore(self.max_concurrency))

    @classmethod
    def from_env(cls) -> RateLimitPolicy:
        return cls(
            max_concurrency=int(os.environ.get("LLM_MAX_CONCURRENCY", "1")),
            max_retries=int(os.environ.get("LLM_MAX_RETRIES", "6")),
            transient_retries=int(os.environ.get("LLM_TRANSIENT_RETRIES", "3")),
            max_hint=float(os.environ.get("LLM_MAX_RETRY_HINT_SECONDS", "60")),
            base_delay=float(os.environ.get("LLM_RETRY_BASE_SECONDS", "5")),
            max_delay=float(os.environ.get("LLM_RETRY_MAX_SECONDS", "120")),
        )

    def call(self, fn: Callable[[], str], model: str = "") -> str:
        with self._gate:
            until = self._exhausted.get(model)
            if until is not None:
                if self._clock() < until:
                    raise QuotaExhausted(
                        f"{model}: 하루 무료 한도 소진 — 약 {(until - self._clock()) / 60:.0f}분 뒤까지 호출하지 않음 "
                        "(429 daily quota; skipped without a request)")
                self._exhausted.pop(model, None)
            limited = transient = 0
            while True:
                try:
                    return fn()
                except Exception as exc:
                    hint = retry_hint_seconds(str(exc)) if is_rate_limited(exc) else None
                    if hint is not None and hint > self.max_hint:
                        self._give_up(model, exc, hint)
                        raise
                    if is_rate_limited(exc) and limited < self.max_retries:
                        delay = self.delay(limited, str(exc))
                        limited += 1
                        logger.warning("%s rate-limited (429/RESOURCE_EXHAUSTED); retry %d/%d in %.0fs",
                                       model, limited, self.max_retries, delay)
                    elif is_transient(exc) and transient < self.transient_retries:
                        delay = self.delay(transient, str(exc))
                        transient += 1
                        logger.warning("%s transient error (%s); retry %d/%d in %.0fs",
                                       model, _short(exc), transient, self.transient_retries, delay)
                    else:
                        raise
                    self._sleep(delay)

    def _give_up(self, model: str, exc: BaseException, hint: float) -> None:
        minutes = hint / 60
        if is_daily_quota(str(exc)):
            limit = re.search(r"quota_value:\s*(\d+)|limit:\s*(\d+)", str(exc))
            count = next((g for g in limit.groups() if g), None) if limit else None
            self._exhausted[model] = self._clock() + hint
            logger.error("%s: Gemini 하루 무료 한도%s를 다 썼습니다 (429, 일일 한도). 약 %.0f분 뒤에 풀립니다. "
                         "재시도하지 않고 이 호출은 기권하며, 그때까지 이 모델의 다른 호출도 요청 없이 기권합니다. "
                         "다른 모델로 바꾸려면 HEDGE_FUND_LLM_MODEL=gemini-flash-lite-latest",
                         model, f"({count}회)" if count else "", minutes)
        else:
            logger.warning("%s: 429 rate-limited and the server asks to wait %.0fs (> %.0fs); "
                           "not retrying — this call abstains", model, hint, self.max_hint)

    def delay(self, attempt: int, message: str = "") -> float:
        backoff = min(self.max_delay, self.base_delay * (2 ** attempt))
        hint = retry_hint_seconds(message)
        if hint is not None:
            backoff = max(backoff, min(self.max_delay, hint))
        return backoff + random.uniform(0, 1)


class QuotaExhausted(RuntimeError):
    """A model's daily quota is known to be spent: the call is not even sent."""


_HINT_TEXT = re.compile(r"retry in\s+((?:\d+(?:\.\d+)?\s*[hms]\s*)+)", re.IGNORECASE)
_HINT_PROTO = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.IGNORECASE)


def retry_hint_seconds(message: str) -> float | None:
    """The wait a 429 asks for, in seconds: "Please retry in 37.2s",
    "retry in 44m10.16s", "retry in 1h2m", or the proto field
    retry_delay { seconds: 2650 }. The longest one found; None when absent."""
    found: list[float] = []
    for match in _HINT_TEXT.finditer(message):
        total = 0.0
        for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", match.group(1), re.IGNORECASE):
            total += float(amount) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
        found.append(total)
    found += [float(m.group(1)) for m in _HINT_PROTO.finditer(message)]
    return max(found) if found else None


def is_daily_quota(message: str) -> bool:
    """The free tier's per-day request quota (not the per-minute one)."""
    text = message.lower()
    return "perday" in text or "per day" in text


def is_rate_limited(exc: BaseException) -> bool:
    """429 / RESOURCE_EXHAUSTED, however the SDK wraps it."""
    text = f"{type(exc).__name__} {exc}"
    return ("429" in text or "RESOURCE_EXHAUSTED" in text or "ResourceExhausted" in text
            or "rate limit" in text.lower())


_TRANSIENT_STATUS = re.compile(r"\b(500|502|503|504)\b")
_TRANSIENT_WORDS = ("deadline expired", "deadline_exceeded", "deadlineexceeded", "unavailable",
                    "serviceunavailable", "internalservererror", "internal server error",
                    "bad gateway", "gateway timeout", "timed out", "timeout", "temporarily",
                    "connection reset", "connection aborted", "remotedisconnected")


def is_transient(exc: BaseException) -> bool:
    """A failure worth retrying: 5xx (500/502/503/504), a deadline or
    timeout, an unavailable server, a dropped connection. Not 429 (that has
    its own budget) and not client errors such as 400/401/403."""
    if is_rate_limited(exc):
        return False
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    text = f"{type(exc).__name__} {exc}"
    return bool(_TRANSIENT_STATUS.search(text)) or any(w in text.lower() for w in _TRANSIENT_WORDS)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:80]}"


class JevLLM:
    """Native TypeSafe adapter returning compatibility JSON without streaming.

    Provider metadata travels with the result so cache replay needs no mutable
    last-response state. Native response bodies are preserved except for echoed
    credentials, which are redacted. Credentials are passed in explicitly.
    """

    ENDPOINT = "https://api.typesafe.ai/v1/systemone"
    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

    def __init__(self, api_key: str, model: str = "jev-1.13.0", timeout: float = 60.0) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A non-empty TypeSafe API key is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout

    def cache_key(self, agent: str, system: str, user: str) -> str:
        """Hash semantic inputs only; do not disturb legacy prompt keys."""
        identity = {
            "provider": "TypeSafe",
            "endpoint": self.ENDPOINT,
            "agent": agent,
            "request": contract.build_jev_request(system, user, self.model),
            "contract_version": contract.JEV_CONTRACT_VERSION,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def complete(self, system: str, user: str) -> str:
        request = contract.build_jev_request(system, user, self.model)
        started = time.monotonic()
        diagnostics = {
            "provider": "TypeSafe",
            "contract_version": contract.JEV_CONTRACT_VERSION,
            "request": request,
            "attempt_count": 0,
            "status_code": None,
            "raw_response": None,
        }

        def fail(category: str, message: str) -> None:
            diagnostics.update(failure_category=category, elapsed_seconds=time.monotonic() - started)
            # Do not chain transport exceptions: their message/request can carry
            # credentials. Diagnostics intentionally omit all HTTP headers.
            raise LLMCallError(message, self._redact(diagnostics)) from None

        for attempt in range(1, 3):
            diagnostics["attempt_count"] = attempt
            try:
                with requests.post(
                    self.ENDPOINT,
                    json=request,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout,
                    allow_redirects=False,
                ) as response:
                    status = response.status_code
                    raw = response.text
                    retry_after = response.headers.get("Retry-After")
            except requests.RequestException:
                fail("transport", "TypeSafe request failed during transport")

            diagnostics.update(status_code=status, raw_response=raw)
            if status in self._RETRY_STATUSES and attempt == 1:
                delay = _retry_delay(retry_after)
                if delay > self._timeout:
                    fail("http", f"TypeSafe HTTP {status}: retry delay exceeds timeout")
                time.sleep(delay)
                continue
            if not 200 <= status < 300:
                fail("http", f"TypeSafe returned HTTP {status}")
            try:
                native_response = json.loads(raw)
            except ValueError:
                fail("json_decode", "TypeSafe returned invalid JSON")
            try:
                payload, metadata = contract.normalize_jev_response(native_response)
            except contract.JevContractError as exc:
                fail("contract", f"TypeSafe response failed validation: {exc}")

            metadata.update(diagnostics, elapsed_seconds=time.monotonic() - started)
            payload["provider_metadata"] = {"jev": self._redact(metadata)}
            return json.dumps(payload)

        raise AssertionError("unreachable: every attempt returns, retries, or raises")

    def _redact(self, value):
        """Protect against a service echoing the API key in an error body."""
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]")
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value


def _retry_delay(header: str | None) -> float:
    """One-second backoff, honoring valid delta-seconds and HTTP-date values."""
    if header is not None:
        try:
            seconds = float(header)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return 1.0
        if math.isfinite(seconds):
            return max(1.0, seconds)
    return 1.0


def make_llm(
    model: str | None = None,
    timeout: float = 60.0,
    max_tokens: int = 4096,
    on_token: TokenListener = None,
) -> LLMClient:
    """Build the client for a model id, routed by the registry's provider.

    The id comes from the caller, else HEDGE_FUND_LLM_MODEL, else DEFAULT_MODEL — the
    same seam the TUI's picker writes to. Raises with the name of the missing
    environment variable, because that is the only thing the user can act on.
    """
    model = model or os.environ.get("HEDGE_FUND_LLM_MODEL") or _default_model()
    provider = provider_for(model)
    if provider is None and model.startswith("gemini"):
        provider = "Google"  # e.g. a Gemini id newer than the registry
    if provider is None:
        # Unlisted ids still work: a model newer than the registry should not
        # need a code change. Anthropic is the default transport.
        provider = "Anthropic"
    if not is_supported(provider):
        raise ValueError(
            f"No v2 client for {provider} (model {model}). "
            f"Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )

    api_key = _require_key(provider)

    if provider == "TypeSafe":
        # Typed judgments have no generated tokens or output-token budget.
        return JevLLM(api_key=api_key, model=model, timeout=timeout)

    if provider == "Anthropic":
        from langchain_anthropic import ChatAnthropic
        chat = ChatAnthropic(model=model, api_key=api_key, timeout=timeout,
                             max_retries=1, max_tokens=max_tokens)
    elif provider == "OpenAI":
        from langchain_openai import ChatOpenAI
        chat = ChatOpenAI(model=model, api_key=api_key, timeout=timeout,
                          max_retries=1, base_url=os.getenv("OPENAI_API_BASE"))
    elif provider == "DeepSeek":
        from langchain_deepseek import ChatDeepSeek
        chat = ChatDeepSeek(model=model, api_key=api_key, timeout=timeout,
                            max_retries=1)
    elif provider == "Google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        # Flash "thinking" models can take well over a minute on a long
        # prompt; at 60s Google answers 504 "Deadline expired". The request
        # deadline is GEMINI_TIMEOUT_SECONDS (default 180, never below the
        # caller's timeout).
        gemini_timeout = max(timeout, float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "180")))
        chat = ChatGoogleGenerativeAI(model=model, api_key=api_key,
                                      timeout=gemini_timeout, max_retries=1)
        # Free-tier quotas are per minute and per day: one call at a time,
        # exponential backoff on 429 and on 5xx / deadline errors (RateLimitPolicy).
        return ChatLLM(model, chat, on_token, rate_limit=RateLimitPolicy.from_env())
    elif provider == "xAI":
        from langchain_xai import ChatXAI
        chat = ChatXAI(model=model, api_key=api_key, timeout=timeout,
                       max_retries=1)
    elif provider == "Kimi":
        # Moonshot speaks the OpenAI wire format. Default to the international
        # host; mainland users override with MOONSHOT_BASE_URL (v1 does the same).
        from langchain_openai import ChatOpenAI
        chat = ChatOpenAI(
            model=model, api_key=api_key, timeout=timeout, max_retries=1,
            base_url=(os.getenv("MOONSHOT_BASE_URL")
                      or "https://api.moonshot.ai/v1"))
    else:  # pragma: no cover - SUPPORTED_PROVIDERS is checked above
        raise ValueError(f"Unhandled provider {provider}")

    return ChatLLM(model, chat, on_token)


def _default_model() -> str:
    from hedge_fund.data.provider import is_kr_mode

    return KR_DEFAULT_MODEL if is_kr_mode() else DEFAULT_MODEL


def AnthropicLLM(model: str | None = None, **kwargs) -> LLMClient:  # noqa: N802
    """Back-compat shim: v2 was Anthropic-only, and this name is exported.
    Prefer make_llm(), which honours whichever model is selected."""
    return make_llm(model or DEFAULT_MODEL, **kwargs)


def _flatten(content, sep: str = "\n") -> str:
    """One provider's response as plain text.

    Reasoning models (Anthropic extended thinking, and Gemini/DeepSeek in
    their thinking modes) return a LIST of blocks rather than a string. Only
    the text blocks are the answer — a thinking block stringified into the
    payload is prose in front of the JSON, which is exactly what breaks the
    parse. Mirrors v1's extract_json_from_response (src/utils/llm.py:109).

    *sep* joins the blocks: "\\n" for a whole message, "" for a stream chunk
    whose blocks are fragments of one continuing string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return sep.join(parts)
    return "" if content is None else str(content)


def _require_key(provider: str) -> str:
    """The provider's API key, or a failure that names the variable to set."""
    env_var = env_var_for(provider)
    # Kimi accepts either name; v1 reads MOONSHOT_API_KEY first.
    key = (os.getenv("MOONSHOT_API_KEY") if provider == "Kimi" else None)
    key = key or (os.getenv(env_var) if env_var else None)
    if not key:
        raise ValueError(
            f"{env_var} not found. Set it in your .env to use {provider} models."
        )
    return key


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response.

    Tries: ```json fence -> whole string -> first balanced {...} block.
    Raises LLMParseError if nothing parses.
    """
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise LLMParseError(f"no JSON object found in response: {text[:200]!r}")
