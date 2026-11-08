"""Trading costs — what a fill costs beyond the price on the tape.

A real shop splits the cost of a trade into three parts: the commission
per share, half the bid-ask spread paid on every side, and market impact,
the price moving against the order while it trades. This model carries all
three, plus the rule a desk uses to avoid paying them for nothing.

Impact follows the square-root law (Almgren et al. 2005; Tóth, Bouchaud et
al. 2011): an order that is a fraction q of the name's average daily volume
moves the price by about Y * sigma * sqrt(q), where sigma is the name's
daily volatility and Y is a number of order one. Published fits put Y
between 0.5 and 1.0; a shop calibrates it from its own fills. The default
is the conservative end. The order carries its ADV and sigma, so the fill
is priced from data the engine had at the time, not re-fetched later.

The no-trade band is the rebalancing-band rule (Masters 2003): an add
smaller than `min_trade_pct` of equity is not worth the spread it would
pay, so it waits. A trim or a close always goes through: it can only move
the book toward a decision that already passed every limit, and a hard
limit may be waiting on it.

Defaults are a liquid US large/mid cap traded by a small fund: 5 bps of
half-spread per side, $0.005 a share (Interactive Brokers' fixed rate),
Y = 1.0, and a 0.5% band. There is no minimum per order. Zero on every
field means free fills and every trade placed.
"""

from __future__ import annotations

from math import sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class TradingCosts(BaseModel):
    """The cost model a mandate names in its `costs:` block."""

    model_config = ConfigDict(extra="forbid")

    spread_bps: float = Field(
        default=5.0, ge=0, description="half the bid-ask spread, in basis points, paid on every side"
    )
    commission_per_share: float = Field(
        default=0.005, ge=0, description="broker commission per share, in dollars"
    )
    impact_coefficient: float = Field(
        default=1.0, ge=0,
        description="Y in the square-root law: impact = Y * daily volatility * sqrt(order / ADV)",
    )
    # 한국 모드용(거래대금 비례). 기본값 0이면 직렬화에서 빠지므로 기존
    # 미국 mandate의 ledger 해시는 바뀌지 않는다.
    commission_rate: float = Field(
        default=0.0, ge=0, lt=1,
        description="broker commission as a fraction of traded notional, both sides (e.g. 0.00015 = 0.015%)",
    )
    sell_tax_rate: float = Field(
        default=0.0, ge=0, lt=1,
        description="transaction tax as a fraction of notional, charged on sells only (Korean securities transaction tax)",
    )
    min_trade_pct: float = Field(
        default=0.005, ge=0, lt=1,
        description="the no-trade band: an add smaller than this fraction of equity waits; trims and closes always trade",
    )

    def fill_price(
        self, side: Literal["buy", "sell"], mark: float,
        participation: float = 0.0, volatility: float = 0.0,
    ) -> float:
        """A buy pays the half-spread and the impact above *mark*; a sell
        receives the same below. *participation* is the order's share of the
        name's average daily volume, *volatility* the name's daily sigma;
        either at zero prices no impact. A short is a sell and a cover is a
        buy, so the same two lines hold."""
        cost = self.spread_bps / 10_000 + self.impact(participation, volatility)
        return mark * (1 + cost) if side == "buy" else mark * (1 - cost)

    def impact(self, participation: float, volatility: float) -> float:
        """The square-root law, as a fraction of the mark."""
        return self.impact_coefficient * volatility * sqrt(participation)

    def commission(self, quantity: int, notional: float = 0.0, side: Literal["buy", "sell"] = "buy") -> float:
        """Per-share commission, plus (Korean mode) a rate on *notional*
        and a tax on sells. Both rates default to zero."""
        fee = quantity * self.commission_per_share + abs(notional) * self.commission_rate
        if side == "sell":
            fee += abs(notional) * self.sell_tax_rate
        return fee

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        for name in ("commission_rate", "sell_tax_rate"):
            if data.get(name) == 0.0:
                data.pop(name, None)
        return data
