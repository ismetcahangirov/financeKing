"""Pure types. Knows about instruments, bars, signals, orders, fills and positions.

Imports nothing but the standard library -- not pydantic, not sqlalchemy, not even
`fking.platform`. That is what makes a domain object free to construct anywhere: a
test can build a Position without dragging in config loading and telemetry, and a
pydantic major bump cannot change the meaning of a Fill.

Every type here is frozen and every state transition returns a new object.

Modules not listed in `__all__` are private. `_guards` and `_errors` are shared
internals; the guards are not part of the surface because a caller outside this
package validating a domain value by hand is a caller constructing one by hand, and
the constructor already refuses what the guards refuse.
"""

from fking.domain._errors import DomainError
from fking.domain.codec import JsonValue, decode, encode
from fking.domain.enums import Direction, OrderType, RiskVerdict, Side, TimeInForce, Venue
from fking.domain.instrument import Instrument
from fking.domain.market import Bar, Tick
from fking.domain.money import Balance, Money
from fking.domain.order import Fill, Order
from fking.domain.portfolio import Account, Portfolio
from fking.domain.position import Position, PositionTransition
from fking.domain.risk_decision import RiskDecision
from fking.domain.signal import Signal

__all__: tuple[str, ...] = (
    "Account",
    "Balance",
    "Bar",
    "Direction",
    "DomainError",
    "Fill",
    "Instrument",
    "JsonValue",
    "Money",
    "Order",
    "OrderType",
    "Portfolio",
    "Position",
    "PositionTransition",
    "RiskDecision",
    "RiskVerdict",
    "Side",
    "Signal",
    "Tick",
    "TimeInForce",
    "Venue",
    "decode",
    "encode",
)
