"""Gemini free-tier guard: backoff on 429 / RESOURCE_EXHAUSTED, one call at a time."""

import pytest

from hedge_fund.llm.client import ChatLLM, is_rate_limited, make_llm, RateLimitPolicy


class ResourceExhausted(Exception):
    pass


def test_429_is_retried_with_exponential_backoff_then_succeeds():
    waits, calls = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ResourceExhausted("429 RESOURCE_EXHAUSTED: quota exceeded")
        return "ok"

    policy = RateLimitPolicy(max_retries=5, base_delay=5, max_delay=120, sleep=waits.append)
    assert policy.call(flaky) == "ok"
    assert len(waits) == 2
    assert 5 <= waits[0] < 6 and 10 <= waits[1] < 11  # 5s, 10s (+ <1s jitter)


def test_server_retry_hint_is_honoured_and_capped():
    policy = RateLimitPolicy(base_delay=1, max_delay=60)
    assert 37 <= policy.delay(0, "429 ... Please retry in 37.2s.") < 39
    assert policy.delay(0, "Please retry in 900s") < 62


def test_other_errors_are_not_retried():
    waits = []
    policy = RateLimitPolicy(sleep=waits.append)
    with pytest.raises(ValueError):
        policy.call(lambda: (_ for _ in ()).throw(ValueError("bad request")))
    assert waits == []


def test_exhausted_retries_raise_so_the_agent_abstains():
    waits = []
    policy = RateLimitPolicy(max_retries=2, base_delay=1, sleep=waits.append)

    def always():
        raise RuntimeError("429 Too Many Requests")

    with pytest.raises(RuntimeError):
        policy.call(always)
    assert len(waits) == 2


def test_rate_limit_detection():
    assert is_rate_limited(ResourceExhausted("x"))
    assert is_rate_limited(RuntimeError("Error 429"))
    assert not is_rate_limited(RuntimeError("500 internal"))


def test_concurrency_defaults_to_one(monkeypatch):
    monkeypatch.delenv("LLM_MAX_CONCURRENCY", raising=False)
    assert RateLimitPolicy.from_env().max_concurrency == 1


def test_gemini_gets_the_policy_and_korean_mode_defaults_to_free_flash(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("HEDGE_FUND_LLM_MODEL", raising=False)
    monkeypatch.setenv("DATA_PROVIDER", "kr")
    llm = make_llm()
    assert isinstance(llm, ChatLLM) and llm.model == "gemini-flash-latest"
    assert isinstance(llm._rate_limit, RateLimitPolicy)
    # an id newer than the registry still routes to Google
    assert make_llm("gemini-9-flash")._rate_limit is not None


def test_us_default_is_unchanged(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("HEDGE_FUND_LLM_MODEL", raising=False)
    llm = make_llm()
    assert llm.model == "claude-opus-5-5" and llm._rate_limit is None
