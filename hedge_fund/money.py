"""Money as the user's market writes it: dollars in US mode, 원 in Korean mode.

Every amount the program prints — NAV, cash, P&L, costs, prices, capital —
goes through `money`, so DATA_PROVIDER=kr never shows "$15,287" for a won
figure. US output is byte-for-byte what it was ("$1,234.56", "$+75")."""

from __future__ import annotations

from hedge_fund.data.provider import is_kr_mode


def money(value: float, decimals: int = 0, sign: bool = False) -> str:
    """US: ``$1,234.56`` (``$+1,234`` with *sign*). Korean: ``1,235원``
    (``+1,235원``); won are shown whole, *decimals* is ignored."""
    plus = "+" if sign else ""
    if is_kr_mode():
        return f"{value:{plus},.0f}원"
    return f"${value:{plus},.{decimals}f}"


def money_short(value: float) -> str:
    """Compact, for chart labels. US: ``$1,235k``. Korean: ``1.2억원`` / ``1,235만원``."""
    if is_kr_mode():
        if abs(value) >= 100_000_000:
            return f"{value / 100_000_000:,.1f}억원"
        if abs(value) >= 10_000:
            return f"{value / 10_000:,.0f}만원"
        return f"{value:,.0f}원"
    if abs(value) >= 10_000:
        return f"${value / 1000:,.0f}k"
    return f"${value:,.0f}"


def pnl(value: float) -> str:
    """A signed P&L figure for tables. US keeps its bare ``+2,040``; Korean
    mode says ``+2,040원``."""
    return money(value, sign=True) if is_kr_mode() else f"{value:+,.0f}"
