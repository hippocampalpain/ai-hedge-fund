"""Which data provider a run uses — the one switch for Korean mode.

``DATA_PROVIDER=kr`` selects the Korean provider (OpenDART + pykrx /
FinanceDataReader). Anything else, or unset, keeps the original US
provider (Financial Datasets) exactly as it was.

Every place that used to build ``FDClient()`` directly calls
``make_data_client()`` instead, so the switch lives here only.

이 모듈은 가볍게 유지한다: 한국 모드 패키지(``hedge_fund.data.kr``)는
한국 모드일 때만 import 된다. 미국 모드에서는 pykrx 등을 아예 불러오지 않는다.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

PROVIDER_ENV = "DATA_PROVIDER"
DART_KEY_ENV = "DART_API_KEY"

NEW_YORK = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")


def data_provider() -> str:
    """``"kr"`` in Korean mode, else ``"us"``. Read on every call, so a
    ``.env`` loaded at startup (apply_credentials) is honoured."""
    return "kr" if os.environ.get(PROVIDER_ENV, "").strip().lower() == "kr" else "us"


def is_kr_mode() -> bool:
    return data_provider() == "kr"


def market_timezone() -> ZoneInfo:
    """The exchange's clock: a daily session is complete once its local date
    has passed. New York for US data, Seoul for KRX."""
    return SEOUL if is_kr_mode() else NEW_YORK


def make_data_client():
    """The raw (uncached) client for the selected provider. Use it exactly
    like ``FDClient()``: as a context manager, wrapped in CachedDataClient."""
    if is_kr_mode():
        from hedge_fund.data.kr.client import KRDataClient

        return KRDataClient()
    from hedge_fund.data.client import FDClient

    return FDClient()
