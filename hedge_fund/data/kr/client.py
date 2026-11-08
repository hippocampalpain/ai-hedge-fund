"""KRDataClient — the DataClient protocol over OpenDART + KRX prices.

Returns the exact models the US client returns (hedge_fund/data/models.py),
so agents, the pipeline and the cache work unchanged. Tickers are 6-digit
KRX codes (``005930``); ``KOSPI`` / ``KOSDAQ`` / ``KOSPI200`` are indices
(use one as a mandate's benchmark).

Point-in-time guarantee for get_financial_metrics(ticker, end_date): only
DART reports with a receipt date (rcept_dt, from rcept_no) on or before
end_date are read — filtering happens *before* any number is derived.

Not available for free, returned empty (agents treat that as "no data"):
news (1차 버전), earnings surprises (no free consensus → PEAD stays neutral).
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date, timedelta

from hedge_fund.data.kr import fundamentals as fx
from hedge_fund.data.kr.accounts import extract_items, flow_items, parse_amount, stock_items
from hedge_fund.data.kr.dart import DartClient, QUARTER_TO_REPORT, receipt_date
from hedge_fund.data.kr.prices import PriceService
from hedge_fund.data.kr.store import KRDataError
from hedge_fund.data.kr.symbols import is_index, SymbolBook
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)

logger = logging.getLogger(__name__)

FIRST_DART_YEAR = 2015  # 전체 재무제표 API는 2015년부터 제공
_logged: set[tuple[str, str]] = set()


def _log_once(kind: str, ticker: str, message: str, *args, level=logging.INFO) -> None:
    if (kind, ticker) not in _logged:
        _logged.add((kind, ticker))
        logger.log(level, message, *args)


class KRDataClient:
    # Disk-cache generation for CachedDataClient. Bump whenever a derived
    # number changes meaning, so answers cached under old formulas are not
    # replayed. 2: newest row priced on the evaluation date; EPS on weighted
    # average issued shares.
    cache_version = "kr-2"

    def __init__(
        self,
        dart: DartClient | None = None,
        prices: PriceService | None = None,
        symbols: SymbolBook | None = None,
    ) -> None:
        self._dart = dart or DartClient()
        self._prices = prices or PriceService()
        self._symbols = symbols or SymbolBook()
        self._corp_codes: dict[str, str] | None = None
        self._reports: dict[tuple[str, int, int], list[fx.Report]] = {}

    # ------------------------------------------------------------------
    # context manager (same shape as FDClient)
    # ------------------------------------------------------------------

    def __enter__(self) -> KRDataClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._dart.close()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def resolve(self, text: str) -> str:
        """6자리 코드 또는 종목명 → 6자리 코드. Falls back to DART's company
        list when the KRX listing cannot be fetched."""
        try:
            return self._symbols.resolve(text)
        except KRDataError:
            wanted = text.strip()
            matches = [r["stock_code"] for r in self._dart.corp_codes() if r["corp_name"] == wanted]
            if len(matches) == 1:
                return matches[0]
            raise

    def corp_code(self, ticker: str) -> str | None:
        if self._corp_codes is None:
            self._corp_codes = {r["stock_code"]: r["corp_code"] for r in self._dart.corp_codes()}
        return self._corp_codes.get(ticker.upper())

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(self, ticker, start_date, end_date, interval="day", interval_multiplier=1) -> list[Price]:
        if interval != "day" or interval_multiplier != 1:
            raise KRDataError("한국 모드는 일봉(interval=day)만 지원합니다 — 분 단위 펀드는 미국 모드 전용입니다.")
        bars = self._prices.bars(ticker, start_date[:10], end_date[:10])
        return [Price(open=b["open"], high=b["high"], low=b["low"], close=b["close"],
                      volume=b["volume"], time=b["date"]) for b in bars]

    def get_snapshot(self, ticker) -> PriceSnapshot | None:
        """Not real time: the latest completed close (a free source has no live quote)."""
        today = date.today()
        bars = self._prices.bars(ticker, (today - timedelta(days=20)).isoformat(), today.isoformat())
        if not bars:
            return None
        last = bars[-1]
        prev = bars[-2]["close"] if len(bars) > 1 else None
        return PriceSnapshot(
            ticker=ticker, price=last["close"], time=last["date"],
            day_change=None if prev is None else last["close"] - prev,
            day_change_percent=None if not prev else (last["close"] / prev - 1) * 100,
        )

    def get_market_cap(self, ticker, end_date) -> float | None:
        bar = self._prices.bar_on_or_before(ticker, end_date)
        return bar.get("market_cap") if bar else None

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10) -> list[FinancialMetrics]:
        if is_index(ticker):
            return []
        corp = self.corp_code(ticker)
        if corp is None:
            _log_once("corp", ticker, "%s: DART 고유번호(corp_code)를 찾지 못해 재무지표를 비워 둡니다.", ticker,
                      level=logging.WARNING)
            return []
        info = self._dart.company(corp) or {}
        if str(info.get("acc_mt") or "12").zfill(2) != "12":
            # 분기말 날짜·사업연도 해석을 12월 결산 기준으로만 검증했다. 추측으로 채우지 않는다.
            _log_once("fy", ticker, "%s: 결산월이 %s월이라 1차 버전에서는 재무지표를 제공하지 않습니다 (12월 결산만 지원).",
                      ticker, info.get("acc_mt"), level=logging.WARNING)
            return []

        end_date = end_date[:10]
        span = 1 if period == "quarterly" else 4
        quarters_needed = limit + span + 4  # window + a year back (growth, averages)
        end_year = int(end_date[:4])
        start_year = max(FIRST_DART_YEAR, end_year - (quarters_needed + 3) // 4 - 1)

        reports = fx.filed_by(self._load_reports(corp, start_year, end_year, end_date), end_date)
        quarters = fx.quarterize(reports, flow_items(), stock_items())
        # 최신 행은 평가일(end_date) 종가로, 과거 행은 각자의 공시일 종가로 밸류에이션한다.
        rows = fx.metrics_rows(ticker, quarters, period, price_at=lambda d: self._price_at(ticker, d),
                               valuation_date=end_date)
        return rows[:limit]

    def _load_reports(self, corp: str, start_year: int, end_year: int, end_date: str) -> list[fx.Report]:
        """Every report for fiscal years [start_year, end_year] whose period
        has ended by *end_date* (an unfinished quarter cannot be filed)."""
        out: list[fx.Report] = []
        shares: float | None = None
        for year in range(start_year, end_year + 1):
            for quarter in (1, 2, 3, 4):
                period_end = f"{year}-{fx.QUARTER_ENDS[quarter]}"
                if period_end >= end_date:
                    break
                key = (corp, year, quarter)
                if key not in self._reports:
                    self._reports[key] = self._fetch_report(corp, year, quarter)
                for report in self._reports[key]:
                    # 1·3분기 보고서에는 주식총수가 없다 → 직전에 공시된 반기·사업보고서 값을 이어 쓴다.
                    if report.shares is not None:
                        shares = report.shares
                    elif shares is not None:
                        report = dataclasses.replace(report, shares=shares)
                    out.append(report)
        return out

    def _fetch_report(self, corp: str, year: int, quarter: int) -> list[fx.Report]:
        reprt = QUARTER_TO_REPORT[quarter]
        fs_div, rows = "CFS", self._dart.financial_statements(corp, year, reprt, "CFS")
        if not rows:  # 연결재무제표가 없는 회사 → 별도재무제표
            fs_div, rows = "OFS", self._dart.financial_statements(corp, year, reprt, "OFS")
        if not rows:
            return []
        filed = receipt_date(rows[0])
        if filed is None:
            return []
        counts = self._dart.share_counts(corp, year, reprt)
        return [fx.Report(year, quarter, filed, fs_div, extract_items(rows, reprt),
                          _outstanding_shares(counts), _issued_shares(counts))]

    def _price_at(self, ticker: str, day: str) -> tuple[float | None, float | None]:
        """Close and market cap on *day* — a filing date, or the evaluation
        date for the newest row — or the last session before it."""
        try:
            bar = self._prices.bar_on_or_before(ticker, day)
        except KRDataError as exc:
            _log_once("price", ticker, "%s: 평가일·공시일 주가를 받지 못해 밸류에이션(PER 등)을 비워 둡니다: %s",
                      ticker, str(exc)[:200], level=logging.WARNING)
            return None, None
        if bar is None:
            return None, None
        return bar["close"], bar.get("market_cap")

    # ------------------------------------------------------------------
    # Company, insiders, news, earnings
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker) -> CompanyFacts | None:
        if is_index(ticker):
            return CompanyFacts(ticker=ticker.upper(), name=ticker.upper(), category="index")
        try:
            row = self._symbols.lookup(ticker)
        except KRDataError as exc:
            _log_once("listing", ticker, "KRX 종목 목록을 받지 못해 업종 정보를 비워 둡니다: %s", str(exc)[:150],
                      level=logging.WARNING)
            row = None
        if row is None:
            return CompanyFacts(ticker=ticker)
        industry = row.get("industry")
        return CompanyFacts(
            ticker=ticker,
            name=row.get("name"),
            # KRX 목록의 Sector는 대부분 비어 있어 업종(KSIC 기준 Industry)을 섹터로 쓴다.
            sector=row.get("sector") or industry,
            industry=industry,
            exchange=row.get("market"),
            location="KR",
        )

    def get_insider_trades(self, ticker, end_date, start_date=None, limit=1000) -> list[InsiderTrade]:
        """DART 임원·주요주주 소유보고 → InsiderTrade. DART gives the change in
        holdings per report, not trade dates or prices: those stay None."""
        corp = self.corp_code(ticker)
        if corp is None:
            return []
        trades = []
        for r in self._dart.insider_reports(corp):
            filed = (r.get("rcept_dt") or "")[:10] or receipt_date(r)
            if not filed or filed > end_date[:10] or (start_date and filed < start_date[:10]):
                continue
            after = parse_amount(r.get("sp_stock_lmp_cnt"))
            change = parse_amount(r.get("sp_stock_lmp_irds_cnt"))
            title = r.get("isu_exctv_ofcps")
            holder = r.get("isu_main_shrholdr")
            trades.append(InsiderTrade(
                ticker=ticker,
                name=r.get("repror") or "",
                filing_date=filed,
                is_board_director=r.get("isu_exctv_rgist_at") == "등기임원",
                issuer=r.get("corp_name"),
                title=title if title and title != "-" else (holder if holder and holder != "-" else None),
                transaction_shares=change,
                shares_owned_before_transaction=None if after is None or change is None else after - change,
                shares_owned_after_transaction=after,
                security_title="특정증권등",
            ))
        trades.sort(key=lambda t: t.filing_date, reverse=True)
        return trades[:limit]

    def get_news(self, ticker, end_date, start_date=None, limit=1000) -> list[CompanyNews]:
        _log_once("news", ticker, "%s: 한국 모드 1차 버전은 뉴스를 제공하지 않아 빈 목록을 돌려줍니다.", ticker)
        return []

    def get_earnings(self, ticker) -> Earnings | None:
        return None

    def get_earnings_history(self, ticker, limit=12) -> list[EarningsRecord]:
        _log_once("earnings", ticker, "%s: 무료 컨센서스가 없어 실적 서프라이즈를 제공하지 않습니다 (PEAD는 중립).", ticker)
        return []

    # ------------------------------------------------------------------
    # Comparison helper (not part of the protocol)
    # ------------------------------------------------------------------

    def krx_reference(self, ticker: str, day: str) -> dict | None:
        """KRX's published PER/PBR/EPS/BPS on or before *day* (pykrx, needs
        KRX login). For checking our numbers, never fed to agents."""
        source = self._prices.pykrx
        if source is None or not source.available():
            return None
        start = f"{int(day[:4]) - 1}{day[4:10]}"
        table = source.fundamentals(ticker, start, day[:10])
        if not table:
            return None
        last = max(table)
        return {"date": last, **table[last]}


def _outstanding_shares(rows: list[dict]) -> float | None:
    """유통주식수(발행주식 − 자기주식), 보통주+우선주 합계. 1·3분기 보고서처럼
    값이 '-'이면 None. 합계 행이 없으면 보통주·우선주를 더한다."""
    total = next((parse_amount(r.get("distb_stock_co")) for r in rows if (r.get("se") or "").strip() == "합계"), None)
    if total is not None and total > 0:
        return total
    parts = [parse_amount(r.get("distb_stock_co")) for r in rows if (r.get("se") or "").strip() in ("보통주", "우선주")]
    parts = [p for p in parts if p is not None]
    return sum(parts) if parts else None


def _issued_shares(rows: list[dict]) -> float | None:
    """발행주식총수(자기주식 포함), 보통주+우선주 합계 — EPS의 가중평균 분모용.
    1·3분기 보고서처럼 값이 '-'이면 None."""
    total = next((parse_amount(r.get("istc_totqy")) for r in rows if (r.get("se") or "").strip() == "합계"), None)
    if total is not None and total > 0:
        return total
    parts = [parse_amount(r.get("istc_totqy")) for r in rows if (r.get("se") or "").strip() in ("보통주", "우선주")]
    parts = [p for p in parts if p is not None]
    return sum(parts) if parts else None
