"""Load .env for v2 tests so FINANCIAL_DATASETS_API_KEY is available."""

import os

import pytest
from dotenv import load_dotenv

from hedge_fund.features import breakpoints

load_dotenv()
# Collection-time checks (skipif on has_data_credential) must see the US
# provider too, not only test bodies: drop DATA_PROVIDER before any test
# module is imported. Korean-mode tests opt in with monkeypatch.setenv.
os.environ.pop("DATA_PROVIDER", None)


@pytest.fixture(autouse=True)
def _us_provider_by_default(monkeypatch):
    """A developer's .env may say DATA_PROVIDER=kr; the suite still tests the
    US provider unless a test opts in with monkeypatch.setenv."""
    monkeypatch.delenv("DATA_PROVIDER", raising=False)


@pytest.fixture(autouse=True)
def _no_breakpoints_download(monkeypatch):
    """A blind agent built without an injected table must not download the
    breakpoints from a test. Tests that need a table pass their own."""
    def refuse():
        raise RuntimeError("tests must inject MEBreakpoints; no network download")
    monkeypatch.setattr(breakpoints, "load_breakpoints", refuse)
    monkeypatch.setattr(breakpoints, "_shared", None)


@pytest.fixture(autouse=True)
def _no_leaked_signal_handlers_or_runner_threads():
    """Every test leaves the process as it found it: the SIGINT/SIGTERM
    handlers it may have replaced are back, and no runner thread (named
    ``aihf-…``) is still alive. A leak here is what turns into a stray
    KeyboardInterrupt in a later test, or after the last one."""
    import signal
    import threading

    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield
    after = {s: signal.getsignal(s) for s in before}
    for sig, handler in before.items():
        signal.signal(sig, handler)  # restore first, so one leak cannot cascade
    leaked = [t.name for t in threading.enumerate() if t.name.startswith("aihf-") and t.is_alive()]
    assert after == before, f"signal handlers not restored: {after} != {before}"
    assert not leaked, f"background threads left running: {leaked}"
