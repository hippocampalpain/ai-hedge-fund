"""DART reports → point-in-time FinancialMetrics rows. Pure functions only.

Pipeline (every step is tested offline in test_kr_fundamentals.py):

1. ``Report``    one DART report: fiscal year, quarter (1–4), filing date
                 (rcept_dt), CFS/OFS, items (flows are YTD cumulative,
                 stocks are period-end balances), shares outstanding.
2. point-in-time ``filed_by(reports, as_of)`` keeps only reports whose
                 filing date ≤ as_of. Everything below is computed from that
                 subset, so no derived number can use a later filing.
3. ``quarterize`` turns cumulative flows into single quarters:
                 Q1 = 1분기 누적, Q2 = 반기 누적 − 1분기 누적,
                 Q3 = 3분기 누적 − 반기 누적, Q4 = 연간 − 3분기 누적.
                 Both reports must use the same statement (CFS/OFS).
4. ``metrics_rows`` sums 4 consecutive quarters (TTM) and computes the
                 ratios below. A row's filing_date is the latest filing date
                 among the reports it used.

Formulas (TTM = 최근 4개 분기 합, 평균 = (이번 기말 + 4분기 전 기말) / 2):

    EPS            = TTM 지배주주순이익 / 유통주식수
    BPS            = 지배주주지분 / 유통주식수
    PER            = 종가 / EPS                 (EPS ≤ 0 이면 None)
    PBR            = 종가 / BPS                 (BPS ≤ 0 이면 None)
    PSR            = 종가 / (TTM 매출 / 유통주식수)
    ROE            = TTM 지배주주순이익 / 평균 지배주주지분
    ROA            = TTM 당기순이익 / 평균 자산총계
    부채비율(D/E)  = 부채총계 / 자본총계        (배수. 네이버의 % 값 ÷ 100)
    유동비율       = 유동자산 / 유동부채
    매출총이익률   = 매출총이익 / 매출 (매출총이익이 없으면 매출 − 매출원가)
    영업이익률     = 영업이익 / 매출,  순이익률 = 당기순이익 / 매출
    FCF            = 영업활동현금흐름 − |유형자산의 취득|
    성장률         = 이번 TTM / 4분기 전 TTM − 1 (기준값 ≤ 0 이면 None)
    EV             = 시가총액 + 차입금(단기·유동성장기·장기·사채 중 있는 것) − 현금성자산
    EBITDA         = 영업이익 + 감가상각비 (+ 무형자산상각비)

Anything whose inputs are missing is None — never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from hedge_fund.data.models import FinancialMetrics

QUARTER_ENDS = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}  # 12월 결산
BORROWINGS = ("short_term_borrowings", "current_portion_lt_debt", "long_term_borrowings", "bonds")


@dataclass
class Report:
    fiscal_year: int
    quarter: int                    # 1..4 (11013, 11012, 11014, 11011)
    filing_date: str                # YYYY-MM-DD, rcept_dt
    fs_div: str                     # "CFS" | "OFS"
    items: dict[str, float | None]  # flows: YTD cumulative; stocks: period end
    shares: float | None = None     # 유통주식수 (발행주식 − 자기주식)

    @property
    def ordinal(self) -> int:
        return self.fiscal_year * 4 + self.quarter - 1

    @property
    def period_end(self) -> str:
        return f"{self.fiscal_year}-{QUARTER_ENDS[self.quarter]}"


@dataclass
class Quarter:
    fiscal_year: int
    quarter: int
    filing_date: str
    fs_div: str
    flows: dict[str, float | None]   # this quarter alone
    stocks: dict[str, float | None]  # period-end balances
    shares: float | None = None

    @property
    def ordinal(self) -> int:
        return self.fiscal_year * 4 + self.quarter - 1

    @property
    def period_end(self) -> str:
        return f"{self.fiscal_year}-{QUARTER_ENDS[self.quarter]}"


@dataclass
class Window:
    """The inputs to one metrics row: summed flows + balances."""

    period_end: str
    filing_date: str
    flows: dict[str, float | None]
    stocks: dict[str, float | None]
    shares: float | None
    prior_stocks: dict[str, float | None] = field(default_factory=dict)  # 4 quarters earlier


# ---------------------------------------------------------------------------
# Point in time
# ---------------------------------------------------------------------------

def filed_by(reports: list[Report], as_of: str) -> list[Report]:
    """Reports public on *as_of*: filing (rcept_dt) on or before it."""
    return [r for r in reports if r.filing_date and r.filing_date <= as_of[:10]]


# ---------------------------------------------------------------------------
# Cumulative → quarterly
# ---------------------------------------------------------------------------

def quarterize(reports: list[Report], flow_keys: list[str], stock_keys: list[str]) -> list[Quarter]:
    """Single-quarter flows by subtracting the previous cumulative report of
    the same fiscal year and the same statement. Oldest first."""
    by_key = {(r.fiscal_year, r.quarter): r for r in reports}
    quarters: list[Quarter] = []
    for r in sorted(reports, key=lambda x: x.ordinal):
        stocks = {k: r.items.get(k) for k in stock_keys}
        if r.quarter == 1:
            flows = {k: r.items.get(k) for k in flow_keys}
            filed = r.filing_date
        else:
            prev = by_key.get((r.fiscal_year, r.quarter - 1))
            if prev is None or prev.fs_div != r.fs_div:
                flows = {k: None for k in flow_keys}
                filed = r.filing_date
            else:
                flows = {k: _sub(r.items.get(k), prev.items.get(k)) for k in flow_keys}
                filed = max(r.filing_date, prev.filing_date)
        quarters.append(Quarter(r.fiscal_year, r.quarter, filed, r.fs_div, flows, stocks, r.shares))
    return quarters


# ---------------------------------------------------------------------------
# Windows (TTM / quarterly / annual)
# ---------------------------------------------------------------------------

def windows(quarters: list[Quarter], period: str = "ttm") -> list[Window]:
    """One Window per quarter that has a full window behind it. Newest first.

    ttm:       the quarter plus the three before it (consecutive, same statement)
    quarterly: the quarter alone
    annual:    ttm windows at fiscal Q4 only
    """
    span = 1 if period == "quarterly" else 4
    by_ord = {q.ordinal: q for q in quarters}
    out: list[Window] = []
    for q in sorted(quarters, key=lambda x: x.ordinal, reverse=True):
        if period == "annual" and q.quarter != 4:
            continue
        members = [by_ord.get(q.ordinal - i) for i in range(span)]
        if any(m is None for m in members) or len({m.fs_div for m in members}) != 1:
            continue
        flows = {k: _sum([m.flows.get(k) for m in members]) for k in q.flows}
        prior = by_ord.get(q.ordinal - 4)
        out.append(Window(
            period_end=q.period_end,
            filing_date=max(m.filing_date for m in members),
            flows=flows,
            stocks=dict(q.stocks),
            shares=q.shares,
            prior_stocks=dict(prior.stocks) if prior is not None and prior.fs_div == q.fs_div else {},
        ))
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

PriceAt = Callable[[str], tuple[float | None, float | None]]  # date → (close, market_cap)


def metrics_rows(
    ticker: str,
    quarters: list[Quarter],
    period: str,
    price_at: PriceAt | None = None,
) -> list[FinancialMetrics]:
    """FinancialMetrics rows, newest first. *price_at(filing_date)* gives the
    close and market cap on the filing date (valuation is as of the day the
    numbers became public); None prices leave valuation fields None."""
    wins = windows(quarters, period)
    by_end = {w.period_end: w for w in wins}
    days = 365.0 * (1 if period == "quarterly" else 4) / 4
    rows: list[FinancialMetrics] = []
    for w in wins:
        prior = by_end.get(_year_earlier(w.period_end))
        close, market_cap = price_at(w.filing_date) if price_at else (None, None)
        rows.append(compute_metrics(ticker, period, w, prior, close, market_cap, days))
    return rows


def compute_metrics(
    ticker: str,
    period: str,
    w: Window,
    prior: Window | None,
    close: float | None,
    market_cap: float | None,
    days: float = 365.0,
) -> FinancialMetrics:
    f, s, ps = w.flows, w.stocks, w.prior_stocks
    shares = w.shares if w.shares and w.shares > 0 else None

    revenue = f.get("revenue")
    ni_parent = _first(f.get("net_income_parent"), f.get("net_income"))
    equity_parent = _first(s.get("equity_parent"), s.get("total_equity"))
    prior_equity_parent = _first(ps.get("equity_parent"), ps.get("total_equity")) if ps else None
    gross = _first(f.get("gross_profit"), _sub(revenue, f.get("cost_of_sales")))
    fcf = _fcf(f)
    ebitda = _ebitda(f)
    debt = _debt(s)
    cash = s.get("cash")

    eps = _div(ni_parent, shares)
    bps = _div(equity_parent, shares)
    sps = _div(revenue, shares)
    ev = None if market_cap is None or debt is None or cash is None else market_cap + debt - cash

    # Growth versus the same window a year earlier
    p_f = prior.flows if prior else {}
    p_shares = prior.shares if prior and prior.shares and prior.shares > 0 else None
    prior_eps = _div(_first(p_f.get("net_income_parent"), p_f.get("net_income")), p_shares)
    eps_growth = _growth(eps, prior_eps)

    inv_turn = _div(f.get("cost_of_sales"), _avg(s.get("inventories"), ps.get("inventories")))
    rec_turn = _div(revenue, _avg(s.get("receivables"), ps.get("receivables")))
    dso = _div(days, rec_turn)
    dio = _div(days, inv_turn)
    wc = _sub(s.get("current_assets"), s.get("current_liabilities"))
    prior_wc = _sub(ps.get("current_assets"), ps.get("current_liabilities"))

    tax_rate = _div(f.get("income_tax"), f.get("pretax_income")) if (f.get("pretax_income") or 0) > 0 else None
    if tax_rate is not None and not 0 <= tax_rate <= 1:
        tax_rate = None
    invested = None if debt is None or cash is None or s.get("total_equity") is None else s["total_equity"] + debt - cash
    nopat = None if tax_rate is None or f.get("operating_income") is None else f["operating_income"] * (1 - tax_rate)

    per = _div(close, eps) if eps is not None and eps > 0 else None

    return FinancialMetrics(
        ticker=ticker,
        report_period=w.period_end,
        period=period,
        currency="KRW",
        filing_date=w.filing_date,
        market_cap=market_cap,
        enterprise_value=ev,
        price_to_earnings_ratio=_r(per),
        price_to_book_ratio=_r(_div(close, bps) if bps is not None and bps > 0 else None),
        price_to_sales_ratio=_r(_div(close, sps) if sps is not None and sps > 0 else None),
        enterprise_value_to_ebitda_ratio=_r(_div(ev, ebitda) if ebitda is not None and ebitda > 0 else None),
        enterprise_value_to_revenue_ratio=_r(_div(ev, revenue) if revenue and revenue > 0 else None),
        free_cash_flow_yield=_r(_div(fcf, market_cap)),
        peg_ratio=_r(per / (eps_growth * 100) if per is not None and eps_growth is not None and eps_growth > 0 else None),
        gross_margin=_r(_div(gross, revenue)),
        operating_margin=_r(_div(f.get("operating_income"), revenue)),
        net_margin=_r(_div(f.get("net_income"), revenue)),
        return_on_equity=_r(_div(ni_parent, _avg(equity_parent, prior_equity_parent))),
        return_on_assets=_r(_div(f.get("net_income"), _avg(s.get("total_assets"), ps.get("total_assets")))),
        return_on_invested_capital=_r(_div(nopat, invested) if invested and invested > 0 else None),
        asset_turnover=_r(_div(revenue, _avg(s.get("total_assets"), ps.get("total_assets")))),
        inventory_turnover=_r(inv_turn),
        receivables_turnover=_r(rec_turn),
        days_sales_outstanding=_r(dso),
        operating_cycle=_r(None if dso is None or dio is None else dso + dio),
        working_capital_turnover=_r(_div(revenue, _avg(wc, prior_wc))),
        current_ratio=_r(_div(s.get("current_assets"), s.get("current_liabilities"))),
        quick_ratio=_r(_div(_sub(s.get("current_assets"), s.get("inventories")), s.get("current_liabilities"))),
        cash_ratio=_r(_div(cash, s.get("current_liabilities"))),
        operating_cash_flow_ratio=_r(_div(f.get("operating_cash_flow"), s.get("current_liabilities"))),
        debt_to_equity=_r(_div(s.get("total_liabilities"), s.get("total_equity"))),
        debt_to_assets=_r(_div(s.get("total_liabilities"), s.get("total_assets"))),
        interest_coverage=_r(_div(f.get("operating_income"), f.get("interest_expense"))
                             if (f.get("interest_expense") or 0) > 0 else None),
        revenue_growth=_r(_growth(revenue, p_f.get("revenue"))),
        earnings_growth=_r(_growth(ni_parent, _first(p_f.get("net_income_parent"), p_f.get("net_income")))),
        book_value_growth=_r(_growth(equity_parent, prior_equity_parent)),
        earnings_per_share_growth=_r(eps_growth),
        free_cash_flow_growth=_r(_growth(fcf, _fcf(p_f) if p_f else None)),
        operating_income_growth=_r(_growth(f.get("operating_income"), p_f.get("operating_income"))),
        ebitda_growth=_r(_growth(ebitda, _ebitda(p_f) if p_f else None)),
        payout_ratio=_r(_div(abs(f["dividends_paid"]), ni_parent)
                        if f.get("dividends_paid") is not None and ni_parent and ni_parent > 0 else None),
        earnings_per_share=_r(eps),
        book_value_per_share=_r(bps),
        free_cash_flow_per_share=_r(_div(fcf, shares)),
    )


# ---------------------------------------------------------------------------
# Small helpers — every one returns None on a missing input
# ---------------------------------------------------------------------------

def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _sum(values: list[float | None]) -> float | None:
    return None if any(v is None for v in values) else sum(values)


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _avg(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else (a + b) / 2


def _first(*values: float | None) -> float | None:
    return next((v for v in values if v is not None), None)


def _growth(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base <= 0:
        return None
    return current / base - 1


def _fcf(f: dict) -> float | None:
    ocf, capex = f.get("operating_cash_flow"), f.get("capex")
    return None if ocf is None or capex is None else ocf - abs(capex)


def _ebitda(f: dict) -> float | None:
    op, dep = f.get("operating_income"), f.get("depreciation")
    if op is None or dep is None:
        return None
    return op + abs(dep) + abs(f.get("amortization") or 0.0)


def _debt(s: dict) -> float | None:
    """Sum of the borrowing lines that exist; None when the statement shows none."""
    found = [s.get(k) for k in BORROWINGS if s.get(k) is not None]
    return sum(found) if found else None


def _year_earlier(period_end: str) -> str:
    return f"{int(period_end[:4]) - 1}{period_end[4:]}"


def _r(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)
