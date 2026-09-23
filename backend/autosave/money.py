"""Money helpers. All amounts are integers in tiyin (1 sum = 100 tiyin)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Union

TIYIN_PER_SUM = 100


def sum_(value: Union[int, str, Decimal]) -> int:
    """Convert an amount in sum (e.g. 23400 or '46350.55') to tiyin."""
    if isinstance(value, float):
        raise TypeError("use int, str or Decimal for money, not float")
    try:
        tiyin = Decimal(value) * TIYIN_PER_SUM
    except InvalidOperation as exc:
        raise ValueError(f"not a money amount: {value!r}") from exc
    if tiyin != tiyin.to_integral_value():
        raise ValueError(f"more precision than 1 tiyin: {value!r}")
    return int(tiyin)
