"""Daily KRX bars from free sources, tried in order:

1. pykrx (KRX 정보데이터시스템). Since 2025 KRX needs a (free) member login:
   set KRX_ID / KRX_PW in .env. Skipped when they are not set. Called with
   ``adjusted=False``: the adjusted path goes to Naver over plain HTTP,
   which proxies that allow HTTPS only cannot carry.
2. FinanceDataReader: stocks from Naver (fchart.stock.naver.com, adjusted
   prices), indices from FDR's GitHub cache (raw.githubusercontent.com).
   Naver's daily *close* is not always the KRX official close: on
   2026-10-20~11-06 (005930) open/high/low/volume (nearly) matched KRX but the close
   differed on 11 of 14 days, while the 전일비 Naver itself shows the next day
   was measured from the KRX close (= pykrx) on all 13 days checkable. So a fallback to FDR is
   logged as a warning — PER/PBR and fills would be on a different close.
3. FDR's GitHub daily KRX snapshot (one CSV per date with close, market cap
   and shares for every stock). Only from about March 2026; stocks only.

A source that raises is recorded and the next one is tried. If no source
returns bars and any source failed, KRDataError is raised (fail loud: the
data may exist, we just could not reach it). Empty everywhere with no
failure means the data genuinely does not exist.

pykrx prints the KRX login ID when it logs in, so every pykrx import and
call runs with stdout/stderr captured and discarded.

Bars are cached on disk per (source, symbol, calendar year). A year that
has ended is final; the current year is refetched when a later completed
date is asked for.
"""

from __future__ import annotations

import contextlib
import csv
import io
import logging
import os
from datetime import date, datetime, timedelta
from typing import Protocol

import requests

from hedge_fund.data.kr.store import DiskCache, KRDataError, Throttle, with_retry
from hedge_fund.data.kr.symbols import INDICES, is_index
from hedge_fund.data.provider import SEOUL

logger = logging.getLogger(__name__)

Bar = dict  # {date, open, high, low, close, volume, market_cap, shares}

SNAPSHOT_URL = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing/krx/{day}.csv"


def kr_completed_through(today: date | None = None) -> str:
    """The last date whose close is final: yesterday in Seoul."""
    today = today or datetime.now(SEOUL).date()
    return (today - timedelta(days=1)).isoformat()


@contextlib.contextmanager
def _quiet():
    """Swallow pykrx/FDR prints (pykrx prints the login ID)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class PriceSource(Protocol):
    name: str

    def available(self) -> bool: ...

    def bars(self, symbol: str, start: str, end: str) -> list[Bar]: ...


# ---------------------------------------------------------------------------
# 1. pykrx
# ---------------------------------------------------------------------------

class PykrxSource:
    name = "pykrx"

    def __init__(self) -> None:
        self._ok: bool | None = None

    def available(self) -> bool:
        if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
            return False
        if self._ok is None:
            try:
                with _quiet():
                    from pykrx.website.comm import auth

                    self._ok = auth.get_auth_session() is not None
            except Exception:
                self._ok = False
            if not self._ok:
                logger.warning("KRX 로그인에 실패했습니다 (KRX_ID/KRX_PW 확인). FinanceDataReader로 대체합니다.")
        return self._ok

    def bars(self, symbol: str, start: str, end: str) -> list[Bar]:
        s, e = start.replace("-", ""), end.replace("-", "")
        with _quiet():
            from pykrx import stock

            if is_index(symbol):
                df = stock.get_index_ohlcv_by_date(s, e, INDICES[symbol.upper()][0], name_display=False)
                caps = None
            else:
                df = stock.get_market_ohlcv_by_date(s, e, symbol, adjusted=False)
                caps = stock.get_market_cap_by_date(s, e, symbol)
        if df is None or df.empty:
            return []
        cap_by_day = {}
        if caps is not None and not caps.empty:
            for idx, row in caps.iterrows():
                cap_by_day[_day(idx)] = (_num(row.get("시가총액")), _num(row.get("상장주식수")))
        out = []
        for idx, row in df.iterrows():
            day = _day(idx)
            cap, shares = cap_by_day.get(day, (None, None))
            out.append(_bar(day, row.get("시가"), row.get("고가"), row.get("저가"), row.get("종가"),
                            row.get("거래량"), cap, shares))
        return [b for b in out if b is not None]

    def fundamentals(self, symbol: str, start: str, end: str) -> dict[str, dict]:
        """KRX's own PER/PBR/EPS/BPS by date — for comparison only."""
        if not self.available():
            return {}
        with _quiet():
            from pykrx import stock

            df = stock.get_market_fundamental_by_date(start.replace("-", ""), end.replace("-", ""), symbol)
        if df is None or df.empty:
            return {}
        return {_day(idx): {k: _num(row.get(k)) for k in ("PER", "PBR", "EPS", "BPS", "DIV")}
                for idx, row in df.iterrows()}


# ---------------------------------------------------------------------------
# 2. FinanceDataReader
# ---------------------------------------------------------------------------

class FdrSource:
    name = "fdr"

    def available(self) -> bool:
        return True

    def bars(self, symbol: str, start: str, end: str) -> list[Bar]:
        target = INDICES[symbol.upper()][1] if is_index(symbol) else symbol
        with _quiet():
            import FinanceDataReader as fdr

            df = fdr.DataReader(target, start, end)
        if df is None or df.empty:
            return []
        out = []
        for idx, row in df.iterrows():
            out.append(_bar(_day(idx), row.get("Open"), row.get("High"), row.get("Low"), row.get("Close"),
                            row.get("Volume"), _num(row.get("MarCap")) if is_index(symbol) else None, None))
        return [b for b in out if b is not None and start <= b["date"] <= end]


# ---------------------------------------------------------------------------
# 3. FDR GitHub daily snapshot
# ---------------------------------------------------------------------------

class SnapshotSource:
    """One CSV per date covering every KRX stock. Each day's file is cached
    once (reduced to the columns used), so all tickers share it."""

    name = "fdr-snapshot"

    def __init__(self, cache: DiskCache | None = None, session: requests.Session | None = None) -> None:
        self._cache = cache or DiskCache("snapshot")
        self._session = session or requests.Session()
        self._throttle = Throttle(0.2)

    def available(self) -> bool:
        return True

    def bars(self, symbol: str, start: str, end: str) -> list[Bar]:
        if is_index(symbol):
            return []
        out = []
        day = date.fromisoformat(start)
        last = date.fromisoformat(end)
        while day <= last:
            if day.weekday() < 5:
                table = self._day_table(day.isoformat())
                row = table.get(symbol.upper()) if table else None
                if row:
                    bar = _bar(day.isoformat(), row["o"], row["h"], row["l"], row["c"], row["v"], row["m"], row["s"])
                    if bar is not None:
                        out.append(bar)
            day += timedelta(days=1)
        return out

    def _day_table(self, day: str) -> dict | None:
        key = self._cache.key("krx", day)
        hit = self._cache.get(key)
        recent = day >= (date.today() - timedelta(days=7)).isoformat()
        if hit is not None and not (recent and hit == {} and self._cache.get(key, max_age=86400) is None):
            return hit

        def fetch() -> requests.Response:
            self._throttle.wait()
            resp = self._session.get(SNAPSHOT_URL.format(day=day), timeout=30)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            return resp

        resp = with_retry(fetch, what=f"KRX snapshot {day}", retry_on=(requests.RequestException,))
        if resp.status_code == 404:
            table: dict = {}
        elif resp.status_code >= 400:
            raise KRDataError(f"KRX snapshot {day}: HTTP {resp.status_code}")
        else:
            table = {}
            for rec in csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))):
                code = (rec.get("Code") or "").strip().upper()
                if code:
                    table[code] = {"o": rec.get("Open"), "h": rec.get("High"), "l": rec.get("Low"),
                                   "c": rec.get("Close"), "v": rec.get("Volume"),
                                   "m": rec.get("Marcap"), "s": rec.get("Stocks")}
        self._cache.put(key, table)
        return table


# ---------------------------------------------------------------------------
# The chain + per-year cache
# ---------------------------------------------------------------------------

class PriceService:
    def __init__(self, sources: list | None = None, cache: DiskCache | None = None,
                 completed_through=kr_completed_through) -> None:
        self._sources = sources if sources is not None else [PykrxSource(), FdrSource(), SnapshotSource()]
        self._cache = cache or DiskCache("prices")
        self._completed = completed_through
        self._memo: dict[tuple, list[Bar]] = {}

    @property
    def pykrx(self) -> PykrxSource | None:
        return next((s for s in self._sources if isinstance(s, PykrxSource)), None)

    def bars(self, symbol: str, start: str, end: str) -> list[Bar]:
        """Bars in [start, end] (completed dates only), oldest first."""
        end = min(end, self._completed())
        if start > end:
            return []
        out: list[Bar] = []
        for year in range(int(start[:4]), int(end[:4]) + 1):
            out.extend(b for b in self._year(symbol, year, end) if start <= b["date"] <= end)
        return out

    def bar_on_or_before(self, symbol: str, day: str, lookback_days: int = 10) -> Bar | None:
        start = (date.fromisoformat(day[:10]) - timedelta(days=lookback_days)).isoformat()
        bars = self.bars(symbol, start, day[:10])
        return bars[-1] if bars else None

    def _year(self, symbol: str, year: int, needed_through: str) -> list[Bar]:
        memo_key = (symbol, year)
        through = min(f"{year}-12-31", needed_through)
        cached = self._memo.get(memo_key)
        if cached is not None and cached[0] >= through:
            return cached[1]
        key = self._cache.key(symbol.upper(), year)
        hit = self._cache.get(key)
        if hit is not None and hit["through"] >= through:
            self._memo[memo_key] = (hit["through"], hit["bars"])
            return hit["bars"]
        fetch_through = min(f"{year}-12-31", self._completed())
        bars, source = self._fetch(symbol, f"{year}-01-01", fetch_through)
        if bars:
            self._cache.put(key, {"through": fetch_through, "source": source, "bars": bars})
        self._memo[memo_key] = (fetch_through, bars)
        return bars

    def _fetch(self, symbol: str, start: str, end: str) -> tuple[list[Bar], str | None]:
        failures: list[str] = []
        for source in self._sources:
            if not source.available():
                continue
            if source.name == "fdr-snapshot":
                # Day-by-day: only ask for the window that can exist.
                start = max(start, "2026-03-01")
                if start > end:
                    continue
            try:
                bars = source.bars(symbol, start, end)
            except Exception as exc:
                failures.append(f"{source.name}: {type(exc).__name__}: {str(exc)[:150]}")
                logger.info("%s failed for %s (%s); trying the next source", source.name, symbol, failures[-1])
                continue
            if bars:
                if source.name == "fdr" and not is_index(symbol):
                    logger.warning("%s: pykrx(KRX) 시세를 쓰지 못해 FinanceDataReader(네이버) 종가로 대체합니다. "
                                   "네이버 종가는 KRX 공식 종가와 다를 수 있습니다 (docs/korea_mode/02_formulas.md 6절).",
                                   symbol)
                return sorted(bars, key=lambda b: b["date"]), source.name
        if failures:
            raise KRDataError(f"{symbol} {start}~{end} 시세를 받지 못했습니다 — " + " | ".join(failures))
        return [], None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _day(idx) -> str:
    return idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _bar(day, o, h, low, c, v, market_cap, shares) -> Bar | None:
    close = _num(c)
    if close is None or close <= 0:  # suspended / weekend snapshot rows show '-' or 0
        return None
    vol = _num(v)
    return {
        "date": day,
        "open": _num(o) or close,
        "high": _num(h) or close,
        "low": _num(low) or close,
        "close": close,
        "volume": int(vol) if vol is not None else 0,
        "market_cap": _num(market_cap),
        "shares": _num(shares),
    }
