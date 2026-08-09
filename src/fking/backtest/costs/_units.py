"""The annotated `Decimal` aliases every calibrated parameter is declared with.

`allow_inf_nan=False` is the load-bearing half. A `Decimal("NaN")` spread propagates
through arithmetic without raising and compares unequal to itself, so one of them turns
every equality-based determinism check into a difference with no cause anybody can find
(`BACKTEST_ENGINE.md` section 5). An infinite spread is worse: it makes every strategy
unprofitable by exactly the amount that hides the bug.

The bounds are stated per alias rather than per field so that the *reason* a bound exists
is written once. A negative spread is not a cheap market, it is a sign error; a negative
depth is not a thin book, it is a parse failure.

There is no `float` alias here and none anywhere in this subpackage. The exception in
`docs/rules/decimal-and-money.md` permits `float` for statistical estimation inside
`fking.backtest`, and this module does not use it: quantiles are selected by nearest rank
rather than interpolated, and the depth walk is linear, so every number the cost model
produces is an exact decimal computation with no boundary to convert at.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final

from pydantic import Field

# Basis points per unit of notional. 1 bp = 0.0001.
BPS_PER_UNIT: Final = Decimal("10000")

# A cost or a spread expressed in basis points, never negative. A negative value here is
# a sign error upstream, and charging it would credit the strategy for trading.
NonNegativeBps = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

# A rate that may legitimately be either sign -- funding is the only one. Longs pay when
# it is positive and are paid when it is negative, and a model that clamped it would
# delete the entire P&L of a carry strategy.
SignedRate = Annotated[Decimal, Field(allow_inf_nan=False)]

# A base-asset quantity that must be strictly positive: a depth level of zero is not a
# thin book, it is a profile that was never populated.
PositiveBaseQuantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
