"""Dollar amounts as exact decimals, stored as integer micro-dollars.

Counters live in Redis, where only integers increment atomically, so every
amount is converted to millionths of a dollar on the way in. A typical
small-model call costs a fraction of a cent: whole cents would round most
calls to zero and the budget would never fill.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

Amount = Decimal | int | float | str
"""Anything accepted where a dollar amount is expected."""

_MICROS = Decimal(1_000_000)


def to_decimal(value: Amount, name: str = "amount") -> Decimal:
    """Convert ``value`` to a finite ``Decimal``.

    Floats go through ``repr`` so ``0.1`` becomes ``Decimal("0.1")`` rather
    than the binary expansion of the float.
    """
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, not bool")
    if not isinstance(value, (Decimal, int, float, str)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    try:
        result = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} is not a number: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return result


def to_micros(usd: Decimal, *, round_up: bool = True) -> int:
    """Dollars to integer micro-dollars.

    Charges round up and limits round down, so rounding never lets spend
    through. The error is below one micro-dollar per operation.
    """
    rounding = ROUND_CEILING if round_up else ROUND_FLOOR
    return int((usd * _MICROS).to_integral_value(rounding=rounding))


def from_micros(micros: int) -> Decimal:
    """Integer micro-dollars back to dollars."""
    return Decimal(micros) / _MICROS
