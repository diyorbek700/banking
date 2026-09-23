"""Push notification texts."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

from .money import TIYIN_PER_SUM


def format_sum(tiyin: int) -> str:
    """2340000 -> '23 400 сум', 159950 -> '1 599,50 сум'."""
    sign = "−" if tiyin < 0 else ""
    whole, frac = divmod(abs(tiyin), TIYIN_PER_SUM)
    text = f"{whole:,}".replace(",", " ")
    if frac:
        text += f",{frac:02d}"
    return f"{sign}{text} сум"


def format_rate(rate: Union[str, Decimal]) -> str:
    """'18.00' -> '18', '18.50' -> '18,5'."""
    value = Decimal(rate).normalize()
    text = format(value, "f")
    return text.replace(".", ",")


def payment_push_text(
    payment_amount: int,
    *,
    round_up_amount: int = 0,
    interest_rate_annual: Optional[str] = None,
    deposit_balance: Optional[int] = None,
    safety_threshold_skip: Optional[int] = None,
) -> str:
    """One combined push for a card payment and its round-up.

    Only the payment amount, the round-up and the savings balance are shown; the card
    balance is never included, because pushes are visible on the lock screen.
    """
    text = f"Оплата {format_sum(payment_amount)}."
    if round_up_amount and deposit_balance is not None and interest_rate_annual is not None:
        text += (
            f" В копилку ({format_rate(interest_rate_annual)}% годовых): +{format_sum(round_up_amount)} 🎯"
            f" (Баланс копилки: {format_sum(deposit_balance)})"
        )
    elif safety_threshold_skip is not None:
        text += (
            " Округление в копилку пропущено: на карте должно оставаться"
            f" не меньше {format_sum(safety_threshold_skip)}."
        )
    return text


def delayed_round_up_push_text(round_up_amount: int, interest_rate_annual: str, deposit_balance: int) -> str:
    """Sent when a round-up that failed at payment time succeeds on retry."""
    return (
        f"В копилку ({format_rate(interest_rate_annual)}% годовых): +{format_sum(round_up_amount)} 🎯"
        f" (Баланс копилки: {format_sum(deposit_balance)})"
    )
