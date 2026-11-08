"""Gemini free-tier guard: backoff on 429 / RESOURCE_EXHAUSTED and on transient
5xx / deadline errors, one call at a time."""

import pytest

from hedge_fund.llm.client import ChatLLM, is_rate_limited, is_transient, make_llm, RateLimitPolicy


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


class DeadlineExceeded(Exception):
    pass


@pytest.mark.parametrize("error", [
    DeadlineExceeded("504 Deadline expired before operation could complete."),
    RuntimeError("503 UNAVAILABLE: The model is overloaded."),
    RuntimeError("502 Bad Gateway"),
    TimeoutError("read timed out"),
])
def test_transient_errors_are_retried_with_exponential_backoff(error):
    waits, calls = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise error
        return "ok"

    policy = RateLimitPolicy(base_delay=5, max_delay=120, sleep=waits.append)
    assert policy.call(flaky) == "ok"
    assert len(waits) == 2 and 5 <= waits[0] < 6 and 10 <= waits[1] < 11


def test_transient_errors_stop_after_three_retries():
    waits, calls = [], []

    def always():
        calls.append(1)
        raise DeadlineExceeded("504 Deadline expired before operation could complete.")

    policy = RateLimitPolicy(base_delay=1, sleep=waits.append)
    with pytest.raises(DeadlineExceeded):
        policy.call(always)
    assert len(calls) == 4 and len(waits) == 3  # 1 call + 3 retries (1s, 2s, 4s)
    assert [int(w) for w in waits] == [1, 2, 4]


def test_429_and_transient_budgets_are_separate():
    errors = iter([RuntimeError("504 Deadline expired"), RuntimeError("429 RESOURCE_EXHAUSTED"),
                   RuntimeError("503 UNAVAILABLE"), RuntimeError("429 Too Many Requests")])

    def flaky():
        error = next(errors, None)
        if error:
            raise error
        return "ok"

    waits = []
    policy = RateLimitPolicy(max_retries=2, transient_retries=2, base_delay=1, sleep=waits.append)
    assert policy.call(flaky) == "ok" and len(waits) == 4


def test_transient_detection():
    assert is_transient(DeadlineExceeded("504 Deadline expired before operation could complete."))
    assert is_transient(RuntimeError("500 internal"))
    assert is_transient(ConnectionError("Connection reset by peer"))
    assert not is_transient(RuntimeError("429 RESOURCE_EXHAUSTED"))  # its own budget
    assert not is_transient(ValueError("400 INVALID_ARGUMENT: bad request"))
    assert not is_transient(RuntimeError("403 PERMISSION_DENIED"))


def test_transient_retries_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_TRANSIENT_RETRIES", "5")
    assert RateLimitPolicy.from_env().transient_retries == 5
    monkeypatch.delenv("LLM_TRANSIENT_RETRIES")
    assert RateLimitPolicy.from_env().transient_retries == 3


def test_gemini_request_deadline_is_longer_than_the_default(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_TIMEOUT_SECONDS", raising=False)
    assert make_llm("gemini-flash-latest")._chat.timeout == 180
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "240")
    assert make_llm("gemini-flash-latest")._chat.timeout == 240


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


# The message Google sent when the free tier's daily quota ran out (2026-11-08).
DAILY_QUOTA = """429 You exceeded your current quota, please check your plan and billing details.
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash, description: Generate content requests per day limit exceeded on Free Tier.
Please retry in 44m10.167255338s. [links {
, violations {
  quota_id: "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
  quota_value: 20
}
, retry_delay {
  seconds: 2650
}
]"""


def test_retry_hints_are_read_in_every_form():
    from hedge_fund.llm.client import retry_hint_seconds
    assert retry_hint_seconds("Please retry in 37.2s.") == pytest.approx(37.2)
    assert retry_hint_seconds(DAILY_QUOTA) == pytest.approx(44 * 60 + 10.167255338)
    assert retry_hint_seconds("retry in 1h2m") == 3720
    assert retry_hint_seconds("retry_delay {\n  seconds: 90\n}") == 90
    assert retry_hint_seconds("429 RESOURCE_EXHAUSTED") is None


def test_a_long_server_wait_abstains_at_once_and_logs_the_daily_quota(caplog):
    import logging
    from hedge_fund.llm.client import QuotaExhausted
    calls, waits = [], []
    now = [1000.0]

    def spent():
        calls.append(1)
        raise ResourceExhausted(DAILY_QUOTA)

    policy = RateLimitPolicy(sleep=waits.append, clock=lambda: now[0])
    with caplog.at_level(logging.ERROR), pytest.raises(ResourceExhausted):
        policy.call(spent, "test-daily-model")
    assert len(calls) == 1 and waits == []                        # no backoff, no sleep
    assert "하루 무료 한도(20회)를 다 썼습니다" in caplog.text and "44분" in caplog.text
    # the next call to the same model is refused without a request…
    with pytest.raises(QuotaExhausted, match="하루 무료 한도 소진"):
        policy.call(spent, "test-daily-model")
    assert len(calls) == 1
    # …until the hinted time has passed
    now[0] += 2700
    with pytest.raises(ResourceExhausted):
        policy.call(spent, "test-daily-model")
    assert len(calls) == 2
    RateLimitPolicy._exhausted.clear()


def test_a_long_per_minute_wait_also_abstains_but_does_not_block_the_model():
    calls = []

    def busy():
        calls.append(1)
        raise ResourceExhausted("429 RESOURCE_EXHAUSTED. Please retry in 90s.")

    policy = RateLimitPolicy(sleep=lambda s: None)
    for _ in range(2):
        with pytest.raises(ResourceExhausted):
            policy.call(busy, "test-minute-model")
    assert len(calls) == 2 and "test-minute-model" not in RateLimitPolicy._exhausted


def test_a_short_server_wait_is_still_retried():
    waits, calls = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ResourceExhausted("429 RESOURCE_EXHAUSTED. Please retry in 37.2s.")
        return "ok"

    assert RateLimitPolicy(sleep=waits.append).call(flaky) == "ok"
    assert len(waits) == 1 and 37 <= waits[0] < 39


def test_the_hint_limit_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRY_HINT_SECONDS", "300")
    assert RateLimitPolicy.from_env().max_hint == 300
