"""Korean mode, offline. Every DART / price response here is a hand-built mock,
so this file passes without a network connection or any API key.

Mock company (12월 결산, 연결재무제표). 누적 금액(YTD):

            1분기  반기   3분기  사업보고서
  2023 매출  100   200    300    400        → 분기 100, 100, 100, 100
  2024 매출  110   230    360    500        → 분기 110, 120, 130, 140 (TTM 500)
  2024 지배순이익 12  25  39  54           → TTM 54

  2024 기말: 지배주주지분 500 (2023 기말 400 → 평균 450), 자본총계 510,
             부채총계 255, 유동자산 300, 유동부채 150, 유통주식수 10
  공시일:   1분기 05-15, 반기 08-14, 3분기 11-14, 사업보고서 다음 해 03-10
"""

from __future__ import annotations

import logging

import pytest
import requests

from hedge_fund.data.kr import fundamentals as fx
from hedge_fund.data.kr.accounts import account_map, extract_items, normalize_name, parse_amount
from hedge_fund.data.kr.client import _outstanding_shares, KRDataClient
from hedge_fund.data.kr.dart import DartClient, receipt_date
from hedge_fund.data.kr.prices import PriceService
from hedge_fund.data.kr.store import DiskCache, KRDataError, with_retry
from hedge_fund.data.kr.symbols import SymbolBook
from hedge_fund.data.models import FinancialMetrics

REPORT = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
FILED = {1: "05-15", 2: "08-14", 3: "11-14"}

REVENUE = {2023: [100, 200, 300, 400], 2024: [110, 230, 360, 500]}
NI_PARENT = {2023: [10, 20, 30, 40], 2024: [12, 25, 39, 54]}
NI = {2023: [11, 22, 33, 44], 2024: [13, 27, 42, 58]}
OCF = {2023: [15, 30, 45, 60], 2024: [20, 40, 60, 80]}
CAPEX = {2023: [-5, -10, -15, -20], 2024: [-7, -15, -22, -30]}  # DART shows outflows negative
EQUITY_PARENT = {2023: [370, 380, 390, 400], 2024: [420, 450, 480, 500]}


def filed(year: int, quarter: int) -> str:
    return f"{year + 1}-03-10" if quarter == 4 else f"{year}-{FILED[quarter]}"


def _row(sj, item, amount, add="", reprt="11011", rcept="20250310000001", ord_=1, use_name=False):
    spec = account_map()[item]
    return {
        "rcept_no": rcept, "reprt_code": reprt, "sj_div": sj,
        "account_id": "-표준계정코드 미사용-" if use_name else spec["ids"][0],
        "account_nm": spec["names"][0], "thstrm_amount": amount, "thstrm_add_amount": add,
        "ord": str(ord_), "currency": "KRW",
    }


def statement_rows(year: int, quarter: int, *, use_name: bool = False, revenue: bool = True) -> list[dict]:
    """A DART fnlttSinglAcntAll response for the mock company. Quarterly
    income-statement rows carry the 3-month amount in thstrm_amount and the
    cumulative one in thstrm_add_amount, exactly as DART does."""
    reprt = REPORT[quarter]
    rcept = filed(year, quarter).replace("-", "") + "000123"
    i = quarter - 1
    ep = EQUITY_PARENT[year][i]
    bs = {"total_assets": ep + 10 + 255, "total_liabilities": 255, "total_equity": ep + 10, "equity_parent": ep,
          "current_assets": 300, "current_liabilities": 150, "cash": 40, "inventories": 30, "receivables": 20,
          "short_term_borrowings": 25}
    rows = [_row("BS", k, f"{v:,}", reprt=reprt, rcept=rcept, ord_=n, use_name=use_name) for n, (k, v) in enumerate(bs.items())]

    def flow(sj, item, series, n):
        cum = series[year][i]
        prev = series[year][i - 1] if i else 0
        if sj == "IS" and quarter != 4:
            return _row(sj, item, f"{cum - prev:,}", f"{cum:,}", reprt, rcept, n, use_name)
        return _row(sj, item, f"{cum:,}", "", reprt, rcept, n, use_name)

    if revenue:
        rows.append(flow("IS", "revenue", REVENUE, 1))
    rows += [flow("IS", "net_income", NI, 2), flow("IS", "net_income_parent", NI_PARENT, 3),
             flow("CF", "operating_cash_flow", OCF, 1), flow("CF", "capex", CAPEX, 2)]
    return rows


def share_rows(quarter: int, issued: float | None = None) -> list[dict]:
    """DART's 주식총수: filled in half-year and annual reports only.
    *issued* adds 발행주식총수 (istc_totqy, 자기주식 포함) to the 합계 row."""
    if quarter in (1, 3):
        return [{"se": "합계", "distb_stock_co": "-", "istc_totqy": "-"}, {"se": "비고", "distb_stock_co": "-"}]
    total = {"se": "합계", "distb_stock_co": "10"}
    if issued is not None:
        total["istc_totqy"] = f"{issued:,.0f}"
    return [{"se": "보통주", "distb_stock_co": "8"}, {"se": "우선주", "distb_stock_co": "2"}, total]


class FakeDart:
    """In-memory DartClient: no network."""

    def __init__(self, *, years=(2023, 2024), fs_div="CFS", acc_mt="12", insiders=None, issued=None, **kw):
        self.kw = kw
        self.issued = issued or {}  # (year, quarter) → 발행주식총수
        self.years = years
        self.fs_div = fs_div
        self.acc_mt = acc_mt
        self.insiders = insiders or []
        self.calls: list[tuple] = []

    def corp_codes(self):
        return [{"corp_code": "00000001", "corp_name": "테스트전자", "stock_code": "123450"}]

    def company(self, corp):
        return {"acc_mt": self.acc_mt}

    def financial_statements(self, corp, year, reprt, fs_div):
        self.calls.append((year, reprt, fs_div))
        if year not in self.years or fs_div != self.fs_div:
            return []
        quarter = {v: k for k, v in REPORT.items()}[reprt]
        return statement_rows(year, quarter, **self.kw)

    def share_counts(self, corp, year, reprt):
        if year not in self.years:
            return []
        quarter = {v: k for k, v in REPORT.items()}[reprt]
        return share_rows(quarter, self.issued.get((year, quarter)))

    def insider_reports(self, corp):
        return self.insiders

    def close(self):
        pass


class FakePrices:
    """Every day closes at 54 with a market cap of 540."""

    pykrx = None

    def __init__(self, close=54.0, error: Exception | None = None):
        self.close, self.error = close, error
        self.asked: list[str] = []

    def bar_on_or_before(self, symbol, day, lookback_days=10):
        self.asked.append(day)
        if self.error:
            raise self.error
        return {"date": day, "close": self.close, "market_cap": self.close * 10}

    def bars(self, symbol, start, end):
        return [{"date": start, "open": 1, "high": 1, "low": 1, "close": self.close, "volume": 5}]


def _client(tmp_path, dart=None, prices=None):
    symbols = SymbolBook(cache=DiskCache("s", root=tmp_path),
                         fetch_listing=lambda: [{"code": "123450", "name": "테스트전자", "market": "KOSPI",
                                                 "industry": "반도체 제조업", "sector": None, "settle_month": 12}])
    return KRDataClient(dart=dart or FakeDart(), prices=prices or FakePrices(), symbols=symbols)


@pytest.fixture(autouse=True)
def _kr_mode(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "kr")


# ---------------------------------------------------------------------------
# DART parsing
# ---------------------------------------------------------------------------

def test_quarterly_income_statement_reads_the_cumulative_column():
    rows = statement_rows(2024, 3)
    rev = next(r for r in rows if r["account_id"] == "ifrs-full_Revenue")
    assert (rev["thstrm_amount"], rev["thstrm_add_amount"]) == ("130", "360")  # 3개월, 누적
    assert extract_items(rows, "11014")["revenue"] == 360


def test_cash_flow_is_cumulative_in_thstrm_amount():
    assert extract_items(statement_rows(2024, 2), "11012")["operating_cash_flow"] == 40


def test_account_name_is_the_fallback_when_the_id_is_not_standard():
    rows = statement_rows(2024, 4, use_name=True)
    assert all(r["account_id"] == "-표준계정코드 미사용-" for r in rows)
    items = extract_items(rows, "11011")
    assert items["revenue"] == 500 and items["total_liabilities"] == 255


def test_name_normalization_and_amount_parsing():
    assert normalize_name(" Ⅰ. 매출액 ") == "매출액"
    assert normalize_name("(1) 유동 자산") == "유동자산"
    assert parse_amount("-1,234") == -1234.0
    assert parse_amount("-") is None and parse_amount("") is None and parse_amount(None) is None


def test_receipt_date_is_the_first_eight_digits_of_rcept_no():
    assert receipt_date({"rcept_no": "20250515000123"}) == "2025-05-15"
    assert receipt_date({"rcept_no": ""}) is None


def test_outstanding_shares_prefer_the_total_row_and_skip_dashes():
    assert _outstanding_shares(share_rows(2)) == 10
    assert _outstanding_shares(share_rows(1)) is None
    assert _outstanding_shares([{"se": "보통주", "distb_stock_co": "8"}, {"se": "우선주", "distb_stock_co": "2"}]) == 10


def test_issued_shares_read_the_total_row_or_add_the_classes():
    from hedge_fund.data.kr.client import _issued_shares

    assert _issued_shares(share_rows(2, issued=12)) == 12
    assert _issued_shares(share_rows(1)) is None
    assert _issued_shares([{"se": "보통주", "istc_totqy": "9"}, {"se": "우선주", "istc_totqy": "3"}]) == 12


# ---------------------------------------------------------------------------
# EPS: 지배주주순이익 ÷ 가중평균 발행주식수 (보통+우선, 와이즈리포트 정의)
# ---------------------------------------------------------------------------

# 2023 반기·기말 12주, 2024 반기 11주, 2024 기말 10주 (자기주식 소각으로 감소)
ISSUED = {(2023, 2): 12, (2023, 4): 12, (2024, 2): 11, (2024, 4): 10}


def test_issued_path_interpolates_between_filed_counts_and_carries_the_last():
    reports = [fx.Report(y, q, filed(y, q), "CFS", {}, 10, ISSUED.get((y, q))) for y in (2023, 2024) for q in (1, 2, 3, 4)]
    quarters = fx.quarterize(reports, [], [])
    path = fx.issued_path(quarters)
    o = {(y, q): y * 4 + q - 1 for y in (2023, 2024) for q in (1, 2, 3, 4)}
    assert path[o[(2024, 1)]] == pytest.approx(11.5)   # 2023 기말 12 → 2024 반기 11 사이
    assert path[o[(2024, 3)]] == pytest.approx(10.5)   # 2024 반기 11 → 기말 10 사이
    assert path[o[(2023, 1)]] == 12                    # 첫 값 이전은 첫 값
    # TTM 2024: (12/2 + 11.5 + 11 + 10.5 + 10/2) / 4 = 11
    assert fx.average_issued_shares(path, o[(2024, 4)], 4) == pytest.approx(11.0)


def test_eps_divides_by_the_weighted_average_issued_shares(tmp_path):
    rows = _client(tmp_path, dart=FakeDart(issued=ISSUED)).get_financial_metrics("123450", "2025-06-30")
    m = rows[0]
    assert m.report_period == "2024-12-31"
    assert m.earnings_per_share == pytest.approx(54 / 11)          # not 54 / 10 (기말 유통주식수)
    assert m.price_to_earnings_ratio == pytest.approx(54 / (54 / 11))
    assert m.book_value_per_share == pytest.approx(50.0)           # BPS keeps 기말 유통주식수 (KRX와 같음)


def test_weighted_shares_never_use_a_count_filed_later(tmp_path):
    """As of 2024-12-31 the 2024 사업보고서 (count 10) is not public: the 3분기
    window carries the 반기 count (11) forward instead of interpolating to 10."""
    rows = _client(tmp_path, dart=FakeDart(issued=ISSUED)).get_financial_metrics("123450", "2024-12-31")
    q3 = rows[0]
    assert q3.report_period == "2024-09-30"
    # 2023Q3 12, 2023Q4 12, 2024Q1 11.5, 2024Q2 11, 2024Q3 11 → (6 + 12 + 11.5 + 11 + 5.5) / 4 = 11.5
    ttm_ni = (40 - 30) + 12 + (25 - 12) + (39 - 25)
    assert q3.earnings_per_share == pytest.approx(ttm_ni / 11.5)


def test_eps_falls_back_to_outstanding_shares_without_an_issued_count(tmp_path):
    rows = _client(tmp_path).get_financial_metrics("123450", "2025-06-30")
    assert rows[0].earnings_per_share == pytest.approx(5.4)  # 54 / 10 (유통주식수)


# ---------------------------------------------------------------------------
# Quarter subtraction
# ---------------------------------------------------------------------------

def _reports(years=(2023, 2024)):
    out = []
    for y in years:
        for q in (1, 2, 3, 4):
            out.append(fx.Report(y, q, filed(y, q), "CFS", extract_items(statement_rows(y, q), REPORT[q]), 10))
    return out


def test_q4_is_annual_minus_q3_cumulative():
    quarters = fx.quarterize(_reports(), ["revenue", "net_income_parent"], ["equity_parent"])
    rev_2024 = [q.flows["revenue"] for q in quarters if q.fiscal_year == 2024]
    assert rev_2024 == [110, 120, 130, 140]  # 110 / 230-110 / 360-230 / 500-360
    q4 = next(q for q in quarters if (q.fiscal_year, q.quarter) == (2024, 4))
    assert q4.flows["net_income_parent"] == 54 - 39
    assert q4.filing_date == "2025-03-10"


def test_a_missing_previous_report_leaves_the_quarter_flows_none():
    reports = [r for r in _reports() if (r.fiscal_year, r.quarter) != (2024, 2)]
    quarters = {(q.fiscal_year, q.quarter): q for q in fx.quarterize(reports, ["revenue"], [])}
    assert quarters[(2024, 3)].flows["revenue"] is None  # cannot subtract from a missing 반기
    assert quarters[(2024, 1)].flows["revenue"] == 110


def test_cfs_and_ofs_are_never_subtracted_from_each_other():
    reports = _reports()
    reports[-1] = fx.Report(2024, 4, "2025-03-10", "OFS", reports[-1].items, 10)
    quarters = {(q.fiscal_year, q.quarter): q for q in fx.quarterize(reports, ["revenue"], [])}
    assert quarters[(2024, 4)].flows["revenue"] is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_ttm_metrics_follow_the_documented_formulas(tmp_path):
    rows = _client(tmp_path).get_financial_metrics("123450", "2025-06-30")
    m = rows[0]
    assert (m.report_period, m.filing_date, m.period, m.currency) == ("2024-12-31", "2025-03-10", "ttm", "KRW")
    assert m.return_on_equity == pytest.approx(54 / ((500 + 400) / 2))  # TTM 지배순이익 / 평균 지배지분
    assert m.debt_to_equity == pytest.approx(255 / 510)                # 부채총계 / 자본총계
    assert m.current_ratio == pytest.approx(2.0)
    assert m.earnings_per_share == pytest.approx(5.4)                   # 54 / 10주
    assert m.price_to_earnings_ratio == pytest.approx(10.0)             # 54원 / 5.4
    assert m.book_value_per_share == pytest.approx(50.0)
    assert m.price_to_book_ratio == pytest.approx(54 / 50)
    assert m.revenue_growth == pytest.approx(500 / 400 - 1)
    assert m.net_margin == pytest.approx(58 / 500)
    assert m.free_cash_flow_per_share == pytest.approx((80 - 30) / 10)  # OCF - |capex|
    assert m.market_cap == 540


def test_oldest_row_without_a_year_earlier_has_no_average_based_ratios(tmp_path):
    rows = _client(tmp_path).get_financial_metrics("123450", "2025-06-30")
    oldest = rows[-1]
    assert oldest.report_period == "2023-12-31"
    assert oldest.return_on_equity is None and oldest.revenue_growth is None
    assert oldest.debt_to_equity is not None  # a balance-only ratio still works


def test_quarterly_period_is_the_single_quarter(tmp_path):
    rows = _client(tmp_path).get_financial_metrics("123450", "2025-06-30", period="quarterly")
    assert rows[0].report_period == "2024-12-31"
    assert rows[0].earnings_per_share == pytest.approx(15 / 10)


def test_older_rows_are_priced_on_their_filing_dates(tmp_path):
    prices = FakePrices()
    _client(tmp_path, prices=prices).get_financial_metrics("123450", "2025-06-30")
    assert "2024-11-14" in prices.asked and max(prices.asked) <= "2025-06-30"


class DatedPrices(FakePrices):
    """Close 54 until 2025-05-31, then 108 — so the pricing date is visible."""

    def bar_on_or_before(self, symbol, day, lookback_days=10):
        self.asked.append(day)
        close = 54.0 if day < "2025-06-01" else 108.0
        return {"date": day, "close": close, "market_cap": close * 10}


def test_newest_row_is_repriced_on_the_evaluation_date(tmp_path):
    """PER/PBR/PSR = 평가일 종가 ÷ 평가일까지 공시된 최신 재무 — not frozen at the filing date."""
    client = _client(tmp_path, prices=DatedPrices())
    may = client.get_financial_metrics("123450", "2025-05-30")[0]
    june = client.get_financial_metrics("123450", "2025-06-30")[0]
    assert may.report_period == june.report_period == "2024-12-31"   # same filed financials
    assert may.price_to_earnings_ratio == pytest.approx(54 / 5.4)
    assert june.price_to_earnings_ratio == pytest.approx(108 / 5.4)  # re-priced on 06-30
    assert june.price_to_book_ratio == pytest.approx(108 / 50)
    assert june.price_to_sales_ratio == pytest.approx(108 / (500 / 10))
    assert june.market_cap == 1080


def test_history_rows_keep_their_filing_date_valuation(tmp_path):
    rows = _client(tmp_path, prices=DatedPrices()).get_financial_metrics("123450", "2025-06-30")
    assert rows[1].report_period == "2024-09-30"
    assert rows[1].market_cap == 540  # priced on its own filing date (2024-11-14), close 54


# ---------------------------------------------------------------------------
# Point in time (look-ahead bias)
# ---------------------------------------------------------------------------

def test_a_report_is_invisible_until_its_receipt_date(tmp_path):
    client = _client(tmp_path)
    before = client.get_financial_metrics("123450", "2025-03-09")
    on = client.get_financial_metrics("123450", "2025-03-10")
    assert before[0].report_period == "2024-09-30"  # 사업보고서(3/10 접수)는 아직 없음
    assert on[0].report_period == "2024-12-31"
    assert all(m.filing_date <= "2025-03-09" for m in before)


def test_filed_by_filters_on_receipt_date_not_period_end():
    reports = _reports()
    visible = fx.filed_by(reports, "2024-05-14")
    assert (2024, 1) not in {(r.fiscal_year, r.quarter) for r in visible}  # 1분기 끝났지만 5/15 접수 전
    assert (2023, 4) in {(r.fiscal_year, r.quarter) for r in visible}


def test_unfinished_periods_are_not_even_requested(tmp_path):
    dart = FakeDart()
    _client(tmp_path, dart=dart).get_financial_metrics("123450", "2024-08-01")
    assert all(year < 2024 or reprt in ("11013", "11012") for year, reprt, _ in dart.calls)
    assert (2024, "11014", "CFS") not in dart.calls


# ---------------------------------------------------------------------------
# CFS → OFS, shares, fiscal year
# ---------------------------------------------------------------------------

def test_separate_statements_are_used_when_there_is_no_consolidated_one(tmp_path):
    rows = _client(tmp_path, dart=FakeDart(fs_div="OFS")).get_financial_metrics("123450", "2025-06-30")
    assert rows and rows[0].return_on_equity == pytest.approx(54 / 450)


def test_q1_and_q3_carry_the_last_filed_share_count(tmp_path):
    rows = _client(tmp_path).get_financial_metrics("123450", "2024-12-31")
    q3 = next(m for m in rows if m.report_period == "2024-09-30")
    assert q3.earnings_per_share is not None  # 3분기 보고서에는 주식총수가 없지만 반기 값을 이어 씀


def test_non_december_fiscal_year_returns_nothing_instead_of_guessing(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        rows = _client(tmp_path, dart=FakeDart(acc_mt="03")).get_financial_metrics("123450", "2025-06-30")
    assert rows == [] and "결산월" in caplog.text


def test_unknown_ticker_and_index_have_no_metrics(tmp_path):
    client = _client(tmp_path)
    assert client.get_financial_metrics("999990", "2025-06-30") == []
    assert client.get_financial_metrics("KOSPI", "2025-06-30") == []


# ---------------------------------------------------------------------------
# None never crashes an agent
# ---------------------------------------------------------------------------

def test_missing_revenue_makes_margins_none_not_zero(tmp_path):
    rows = _client(tmp_path, dart=FakeDart(revenue=False)).get_financial_metrics("123450", "2025-06-30")
    m = rows[0]
    assert m.gross_margin is None and m.net_margin is None and m.revenue_growth is None
    assert m.return_on_equity is not None


def test_unreachable_prices_leave_valuation_none(tmp_path):
    rows = _client(tmp_path, prices=FakePrices(error=KRDataError("offline"))).get_financial_metrics("123450", "2025-06-30")
    assert rows[0].price_to_earnings_ratio is None and rows[0].market_cap is None
    assert rows[0].return_on_equity is not None


def test_an_llm_agent_reasons_over_korean_metrics_full_of_none(tmp_path):
    from hedge_fund.features.breakpoints import NoBreakpoints, shared_breakpoints
    from hedge_fund.llm import PromptCache
    from hedge_fund.signals import GrahamAgent

    class FakeLLM:
        model = "fake"
        prompts: list[str] = []

        def complete(self, system, user):
            self.prompts.append(user)
            return '{"signal": "bullish", "confidence": 60, "reasoning": "ok"}'

    assert isinstance(shared_breakpoints(), NoBreakpoints)  # no USD size table in KR mode
    llm = FakeLLM()
    client = _client(tmp_path, dart=FakeDart(revenue=False), prices=FakePrices(error=KRDataError("offline")))
    agent = GrahamAgent(llm=llm, cache=PromptCache(tmp_path / "llm"), blind=True)
    signal = agent.predict("123450", "2025-06-30", client)
    assert signal.metadata["abstained"] is False and signal.value == pytest.approx(0.6)
    assert "US stocks" not in llm.prompts[0]


# ---------------------------------------------------------------------------
# Insiders, news, facts
# ---------------------------------------------------------------------------

def test_insider_reports_map_to_insider_trades(tmp_path):
    rows = [
        {"rcept_no": "20240201000001", "rcept_dt": "2024-02-01", "corp_name": "테스트전자", "repror": "홍길동",
         "isu_exctv_rgist_at": "등기임원", "isu_exctv_ofcps": "대표이사", "isu_main_shrholdr": "-",
         "sp_stock_lmp_cnt": "1,200", "sp_stock_lmp_irds_cnt": "200"},
        {"rcept_no": "20250101000001", "rcept_dt": "2025-01-01", "repror": "미래", "sp_stock_lmp_cnt": "1"},
    ]
    trades = _client(tmp_path, dart=FakeDart(insiders=rows)).get_insider_trades("123450", "2024-12-31")
    assert len(trades) == 1  # the 2025 filing is after end_date
    t = trades[0]
    assert (t.name, t.title, t.is_board_director, t.filing_date) == ("홍길동", "대표이사", True, "2024-02-01")
    assert (t.transaction_shares, t.shares_owned_before_transaction, t.shares_owned_after_transaction) == (200, 1000, 1200)
    assert t.transaction_price_per_share is None  # DART does not report it; never invented


def test_news_is_empty_and_says_so(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert _client(tmp_path).get_news("123456", "2025-01-01") == []
    assert "뉴스" in caplog.text


def test_company_facts_and_name_resolution(tmp_path):
    client = _client(tmp_path)
    facts = client.get_company_facts("123450")
    assert (facts.name, facts.sector, facts.exchange) == ("테스트전자", "반도체 제조업", "KOSPI")
    assert client.resolve("테스트 전자") == "123450"
    assert client.resolve("005930") == "005930"
    assert client.resolve("kospi") == "KOSPI"
    with pytest.raises(ValueError):
        client.resolve("없는회사")


# ---------------------------------------------------------------------------
# Prices: source chain and fail-loud
# ---------------------------------------------------------------------------

class Source:
    def __init__(self, name, bars=None, error=None):
        self.name, self._bars, self._error = name, bars or [], error

    def available(self):
        return True

    def bars(self, symbol, start, end):
        if self._error:
            raise self._error
        return self._bars


def _bar(day, close=100.0):
    return {"date": day, "open": close, "high": close, "low": close, "close": close, "volume": 1,
            "market_cap": None, "shares": None}


def _service(tmp_path, *sources):
    return PriceService(list(sources), cache=DiskCache("p", root=tmp_path), completed_through=lambda: "2025-12-31")


def test_the_next_source_is_tried_when_one_fails(tmp_path):
    svc = _service(tmp_path, Source("pykrx", error=RuntimeError("login")), Source("fdr", [_bar("2025-01-02")]))
    assert [b["date"] for b in svc.bars("005930", "2025-01-01", "2025-01-31")] == ["2025-01-02"]


def test_every_source_failing_raises_instead_of_returning_empty(tmp_path):
    svc = _service(tmp_path, Source("pykrx", error=RuntimeError("down")), Source("fdr", error=OSError("blocked")))
    with pytest.raises(KRDataError):
        svc.bars("005930", "2025-01-01", "2025-01-31")


def test_genuinely_no_data_is_an_empty_list(tmp_path):
    assert _service(tmp_path, Source("fdr")).bars("005930", "2025-01-01", "2025-01-31") == []


def test_bars_are_cached_per_year(tmp_path):
    first = Source("fdr", [_bar("2025-01-02"), _bar("2025-06-02")])
    _service(tmp_path, first).bars("005930", "2025-01-01", "2025-01-31")
    again = _service(tmp_path, Source("fdr", error=RuntimeError("must not be called")))
    assert [b["date"] for b in again.bars("005930", "2025-06-01", "2025-06-30")] == ["2025-06-02"]


def test_prices_reach_the_protocol_model(tmp_path):
    client = KRDataClient(dart=FakeDart(), prices=_service(tmp_path, Source("fdr", [_bar("2025-01-02", 70000)])),
                          symbols=SymbolBook(cache=DiskCache("s", root=tmp_path), fetch_listing=lambda: []))
    (bar,) = client.get_prices("005930", "2025-01-01", "2025-01-31")
    assert (bar.time, bar.close, bar.volume) == ("2025-01-02", 70000, 1)
    with pytest.raises(KRDataError):
        client.get_prices("005930", "2025-01-01", "2025-01-31", interval="minute")


# ---------------------------------------------------------------------------
# Secrets, retries, provider switch
# ---------------------------------------------------------------------------

def test_dart_errors_never_contain_the_api_key(tmp_path, monkeypatch):
    secret = "K" * 40

    class Boom:
        def get(self, url, params=None, timeout=None):
            raise requests.ConnectionError(f"failed: {url}?crtfc_key={params['crtfc_key']}")

        def close(self):
            pass

    dart = DartClient(api_key=secret, cache=DiskCache("d", root=tmp_path), min_interval=0, session=Boom())
    monkeypatch.setattr("hedge_fund.data.kr.store.time.sleep", lambda s: None)
    with pytest.raises(KRDataError) as info:
        dart.financial_statements("00126380", 2024, "11011", "CFS")
    assert secret not in str(info.value) and "***" in str(info.value)


def test_dart_no_data_status_is_an_empty_list_and_bad_key_is_loud(tmp_path):
    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class Session:
        def __init__(self, body):
            self.body = body

        def get(self, *a, **k):
            return Resp(self.body)

        def close(self):
            pass

    ok = DartClient(api_key="x", cache=DiskCache("a", root=tmp_path), min_interval=0,
                    session=Session({"status": "013", "message": "조회된 데이타가 없습니다."}))
    assert ok.financial_statements("1", 2015, "11011", "CFS") == []
    bad = DartClient(api_key="x", cache=DiskCache("b", root=tmp_path), min_interval=0,
                     session=Session({"status": "010", "message": "등록되지 않은 키입니다."}))
    with pytest.raises(KRDataError):
        bad.financial_statements("1", 2015, "11011", "CFS")


def test_with_retry_backs_off_exponentially():
    waits, attempts = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("temporary")
        return "ok"

    assert with_retry(flaky, what="x", base_delay=2, sleep=waits.append) == "ok"
    assert waits == [2, 4]


def test_provider_switch(monkeypatch):
    from hedge_fund.data.client import FDClient
    from hedge_fund.data.provider import make_data_client, market_timezone
    from hedge_fund.data.sessions import completed_through
    from datetime import datetime, timezone

    assert isinstance(make_data_client(), KRDataClient)
    # 2025-01-02 00:30 KST = 2025-01-01 15:30 UTC: Seoul has finished 01-01, New York has not.
    now = datetime(2025, 1, 1, 15, 30, tzinfo=timezone.utc)
    assert str(market_timezone()) == "Asia/Seoul" and completed_through(now) == "2025-01-01"
    monkeypatch.delenv("DATA_PROVIDER")
    assert isinstance(make_data_client(), FDClient) and completed_through(now) == "2024-12-31"


def test_kr_metrics_are_the_same_model_the_agents_read(tmp_path):
    rows = _client(tmp_path).get_financial_metrics("123450", "2025-06-30")
    assert all(isinstance(m, FinancialMetrics) for m in rows)


# ---------------------------------------------------------------------------
# End to end: the unchanged backtest engine over the Korean client
# ---------------------------------------------------------------------------

def _weekday_bars(start: str, end: str, base: float, step: float) -> list[dict]:
    from datetime import date, timedelta

    out, day, i = [], date.fromisoformat(start), 0
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            close = base + step * i
            out.append({"date": day.isoformat(), "open": close, "high": close, "low": close, "close": close,
                        "volume": 1_000_000, "market_cap": close * 10, "shares": 10.0})
            i += 1
        day += timedelta(days=1)
    return out


class SeriesSource:
    name = "mock"

    def available(self):
        return True

    def bars(self, symbol, start, end):
        if symbol == "KOSPI":
            return _weekday_bars(start, end, 2500.0, 1.0)
        return _weekday_bars(start, end, 54.0, 0.1)


def test_a_two_week_kospi_backtest_runs_end_to_end_with_mocks(tmp_path):
    from hedge_fund.backtesting import backtest_fund
    from hedge_fund.data import CachedDataClient
    from hedge_fund.fund import Fund, load_spec
    from hedge_fund.llm import PromptCache
    from hedge_fund.signals import BuffettAgent, GrahamAgent
    from pathlib import Path

    class FakeLLM:
        model = "fake"
        calls = 0

        def complete(self, system, user):
            FakeLLM.calls += 1
            return '{"signal": "bullish", "confidence": 80, "reasoning": "margin of safety"}'

    spec = load_spec(Path(__file__).resolve().parents[2] / "fund" / "example_kr.yaml")
    spec = spec.model_copy(update={"strategies": [spec.strategies[0].model_copy(update={
        "models": [m for m in spec.strategies[0].models if m.name in ("graham", "buffett")]})]})
    llm, cache = FakeLLM(), PromptCache(tmp_path / "llm")
    fund = Fund(spec, blind=True, models={"deep-value": [
        GrahamAgent(llm=llm, cache=cache, blind=True), BuffettAgent(llm=llm, cache=cache, blind=True)]})
    prices = PriceService([SeriesSource()], cache=DiskCache("p", root=tmp_path), completed_through=lambda: "2025-12-31")
    client = _client(tmp_path, prices=prices)
    data = CachedDataClient(client, cache_dir=tmp_path / "cache")

    result = backtest_fund(fund, "2025-06-02", "2025-06-13", data, ["123450"])

    assert result.benchmark == "KOSPI" and len(result.dates) == 10
    assert result.metrics.n_orders >= 1  # bullish views became a position
    assert all(r.positions.get("123450", 0) >= 0 for r in result.records)
    # 2 agents × 1 ticker × 2 rebalances (06-02, 06-09): the newest row is re-priced
    # on each evaluation date, so the second rebalance is a new prompt, not a cache hit.
    assert FakeLLM.calls == 4

    # The pre-run estimate printed by `aihf backtest` matches what actually happened.
    from hedge_fund.backtesting.fund import build_schedule
    from hedge_fund.run import llm_call_estimate
    schedule = build_schedule(data, "KOSPI", "2025-06-02", "2025-06-13", spec.rebalance)
    assert schedule.assessment_dates == ["2025-06-02", "2025-06-09"]
    assert llm_call_estimate(spec, ["123450"], schedule)["calls"] == FakeLLM.calls


def test_a_friday_start_asks_the_llm_on_trading_days_only(tmp_path):
    """The 10-23 (Fri) → 10-26 (Mon) case: no call is made for Sunday 10-25."""
    from pathlib import Path

    from hedge_fund.backtesting import backtest_fund
    from hedge_fund.data import CachedDataClient
    from hedge_fund.fund import Fund, load_spec
    from hedge_fund.llm import PromptCache
    from hedge_fund.signals import GrahamAgent

    class FakeLLM:
        model = "fake"

        def complete(self, system, user):
            return '{"signal": "neutral", "confidence": 50, "reasoning": "x"}'

    spec = load_spec(Path(__file__).resolve().parents[2] / "fund" / "example_kr.yaml")
    spec = spec.model_copy(update={"strategies": [spec.strategies[0].model_copy(update={
        "models": [m for m in spec.strategies[0].models if m.name == "graham"]})]})
    asked: list[str] = []

    class Spy(GrahamAgent):
        def predict(self, ticker, date, data_client):
            asked.append(date)
            return super().predict(ticker, date, data_client)

    fund = Fund(spec, blind=True, models={"deep-value": [Spy(llm=FakeLLM(), cache=PromptCache(tmp_path / "llm"), blind=True)]})
    prices = PriceService([SeriesSource()], cache=DiskCache("p", root=tmp_path), completed_through=lambda: "2025-12-31")
    data = CachedDataClient(_client(tmp_path, prices=prices), cache_dir=tmp_path / "cache")
    result = backtest_fund(fund, "2025-06-06", "2025-06-20", data, ["123450"])   # Fri 06-06 start
    assert sorted(set(asked)) == ["2025-06-06", "2025-06-09", "2025-06-16"]
    assert set(asked) <= set(result.dates)


def test_falling_back_to_naver_closes_is_a_warning(tmp_path, caplog):
    class Down:
        name = "pykrx"

        def available(self):
            return True

        def bars(self, symbol, start, end):
            raise RuntimeError("KRX login failed")

    class Naver(SeriesSource):
        name = "fdr"

    service = PriceService([Down(), Naver()], cache=DiskCache("p", root=tmp_path), completed_through=lambda: "2025-06-30")
    with caplog.at_level(logging.WARNING):
        assert service.bars("123450", "2025-06-02", "2025-06-06")
    assert "KRX 공식 종가와 다를 수 있습니다" in caplog.text


def test_korean_client_names_its_metrics_cache_version():
    assert KRDataClient.cache_version == "kr-2"


# ---------------------------------------------------------------------------
# Order path without an LLM: forced views → orders → fills at the pykrx close → P&L vs KOSPI
# ---------------------------------------------------------------------------

DAYS = ["2025-06-02", "2025-06-03", "2025-06-04", "2025-06-05", "2025-06-06",
        "2025-06-09", "2025-06-10", "2025-06-11", "2025-06-12", "2025-06-13"]
STOCK = dict(zip(DAYS, [50_000, 52_000, 51_000, 53_000, 54_000, 55_000, 56_000, 54_500, 53_000, 52_500]))
KOSPI = dict(zip(DAYS, [2_500, 2_510, 2_490, 2_520, 2_530, 2_540, 2_550, 2_545, 2_535, 2_560]))


class FakePykrx:
    """Stands in for PykrxSource: the KRX closes above, nothing else."""

    name = "pykrx"

    def available(self):
        return True

    def bars(self, symbol, start, end):
        series = KOSPI if symbol == "KOSPI" else STOCK
        return [{"date": d, "open": c, "high": c, "low": c, "close": float(c), "volume": 10_000_000,
                 "market_cap": c * 10.0, "shares": 10.0} for d, c in series.items() if start <= d <= end]


def test_forced_views_trade_at_the_krx_close_and_score_against_kospi(tmp_path):
    from pathlib import Path

    from hedge_fund.backtesting import backtest_fund
    from hedge_fund.data import CachedDataClient
    from hedge_fund.fund import Fund, load_spec
    from hedge_fund.models import Signal
    from hedge_fund.signals import GrahamAgent

    class Forced(GrahamAgent):
        """Graham's slot with a fixed view: fully bullish in week 1, flat in week 2. No LLM."""

        def predict(self, ticker, date, data_client):
            value = 1.0 if date < "2025-06-09" else 0.0
            return Signal(model_name="graham", ticker=ticker, date=date, value=value)

    spec = load_spec(Path(__file__).resolve().parents[2] / "fund" / "example_kr.yaml")
    spec = spec.model_copy(update={
        "strategies": [spec.strategies[0].model_copy(update={
            "models": [m for m in spec.strategies[0].models if m.name == "graham"]})],
        # exact arithmetic: fill = close; 0.015% commission both ways, 0.2% tax on sells
        "costs": spec.costs.model_copy(update={"spread_bps": 0, "impact_coefficient": 0,
                                               "commission_rate": 0.00015, "sell_tax_rate": 0.002}),
    })
    fund = Fund(spec, models={"deep-value": [Forced(llm=type("NoLLM", (), {"model": "none"})())]})
    prices = PriceService([FakePykrx()], cache=DiskCache("p", root=tmp_path), completed_through=lambda: "2025-12-31")
    data = CachedDataClient(_client(tmp_path, prices=prices), cache_dir=tmp_path / "cache")

    result = backtest_fund(fund, DAYS[0], DAYS[-1], data, ["123450"])
    cycles = [r.executed for r in result.records if r.executed is not None]

    # Monday 06-02 decides, Tuesday 06-03 fills at Tuesday's KRX close
    buy = cycles[0]
    assert (buy.as_of, buy.execution_as_of) == ("2025-06-02", "2025-06-03")
    (fill,) = buy.fills
    assert (fill.side, fill.price) == ("buy", 52_000)
    assert fill.quantity == int(0.25 * 100_000_000 // 52_000)          # max_position_pct 25%
    cash = 100_000_000 - fill.quantity * 52_000 * (1 + 0.00015)
    assert buy.cash == pytest.approx(cash)

    # Monday 06-09 goes flat, Tuesday 06-10 sells everything at 56,000, paying commission + tax
    sell = cycles[1]
    (out,) = sell.fills
    assert (sell.execution_as_of, out.side, out.quantity, out.price) == ("2025-06-10", "sell", fill.quantity, 56_000)
    cash += fill.quantity * 56_000 * (1 - 0.00015 - 0.002)
    assert sell.cash == pytest.approx(cash) and sell.positions == {}

    m = result.metrics
    assert m.n_orders == 2
    assert result.nav[-1] == pytest.approx(cash)
    assert m.total_return_pct == pytest.approx(cash / 100_000_000 - 1, abs=1e-6)   # metrics round to 6 places
    assert m.benchmark_return_pct == pytest.approx(2_560 / 2_500 - 1, abs=1e-6)
    assert m.excess_return_pct == pytest.approx(m.total_return_pct - m.benchmark_return_pct, abs=1e-6)
    assert m.trading_cost == pytest.approx(fill.quantity * (52_000 * 0.00015 + 56_000 * (0.00015 + 0.002)))
