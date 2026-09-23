"""Round-up calculation."""
from __future__ import annotations

from .money import sum_

# Steps the product offers: 1 000, 5 000 and 10 000 sum.
ALLOWED_STEPS = (sum_(1000), sum_(5000), sum_(10000))


def calculate_round_up(amount: int, step: int) -> int:
    """Return how much must be added to `amount` to reach the next multiple of `step`.

    Both arguments are in tiyin. A purchase that is already a multiple of the step
    produces no round-up (0), e.g. 23 400 sum with step 5 000 -> 1 600 sum,
    25 000 sum with step 5 000 -> 0.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError("amount must be an int in tiyin")
    if not isinstance(step, int) or isinstance(step, bool):
        raise TypeError("step must be an int in tiyin")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if step <= 0:
        raise ValueError("step must be positive")
    return -amount % step
