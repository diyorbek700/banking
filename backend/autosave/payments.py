"""Card payment processing with a real-time Me-to-Me round-up to the savings deposit.

Pipeline for one card payment (all inside a single ACID transaction):

1. Idempotency: a repeated request with the same key returns the original result.
2. Debit the card for the purchase and post CARD_PAYMENT_ACQUIRING.
3. Decide whether a round-up applies (feature on, deposit active, amount not a multiple
   of the step, and the card keeps at least `safety_balance_threshold` afterwards).
4. Post SAVINGS_ROUND_UP_ME_TO_ME (card -> deposit) inside a SAVEPOINT. If it fails,
   only the savepoint is rolled back: the purchase still commits, a FAILED round-up
   is recorded and a ROUND_UP_RETRY event goes to the outbox (graceful degradation).
5. Enqueue one combined push notification in the outbox.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional, Tuple

from .db import savepoint, transaction
from .journal import enqueue_outbox, post_transaction, write_audit
from .notifications import payment_push_text
from .roundup import calculate_round_up

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentDeclined(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different payment."""


class RoundUpPostingError(Exception):
    """The Me-to-Me posting could not be applied (state changed under us)."""


class RoundUpStatus(str, Enum):
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class SkipReason(str, Enum):
    DISABLED = "DISABLED"
    NO_TARGET_DEPOSIT = "NO_TARGET_DEPOSIT"
    DEPOSIT_BLOCKED = "DEPOSIT_BLOCKED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    AMOUNT_MULTIPLE_OF_STEP = "AMOUNT_MULTIPLE_OF_STEP"
    BELOW_SAFETY_THRESHOLD = "BELOW_SAFETY_THRESHOLD"


@dataclass(frozen=True)
class PaymentRequest:
    user_id: int
    card_account_id: int
    merchant_name: str
    amount: int  # tiyin
    idempotency_key: str


@dataclass(frozen=True)
class PaymentResult:
    payment_transaction_id: int
    amount: int
    round_up_status: RoundUpStatus
    round_up_amount: int = 0
    round_up_transaction_id: Optional[int] = None
    skip_reason: Optional[SkipReason] = None
    deposit_balance: Optional[int] = None
    replayed: bool = False


@dataclass(frozen=True)
class RoundUpDecision:
    amount: int
    skip_reason: Optional[SkipReason]
    deposit: Optional[sqlite3.Row] = None
    safety_balance_threshold: int = 0

    @property
    def eligible(self) -> bool:
        return self.skip_reason is None


class RoundUpEngine:
    """Eligibility rules and the Me-to-Me posting. Shared by the payment flow and the retry worker."""

    def evaluate(self, conn: sqlite3.Connection, user_id: int, card: sqlite3.Row,
                 card_balance: int, payment_amount: int) -> RoundUpDecision:
        """`card_balance` is the card balance after the purchase has been debited."""
        settings = conn.execute(
            "SELECT * FROM auto_savings_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if settings is None or not settings["is_enabled"]:
            return RoundUpDecision(0, SkipReason.DISABLED)

        threshold = settings["safety_balance_threshold"]
        deposit = conn.execute(
            "SELECT * FROM deposit_accounts WHERE id = ? AND user_id = ?",
            (settings["target_deposit_account_id"], user_id),
        ).fetchone()
        if deposit is None:
            return RoundUpDecision(0, SkipReason.NO_TARGET_DEPOSIT, safety_balance_threshold=threshold)

        amount = calculate_round_up(payment_amount, settings["round_up_step"])
        if deposit["status"] != "ACTIVE":
            return RoundUpDecision(amount, SkipReason.DEPOSIT_BLOCKED, deposit, threshold)
        if deposit["currency"] != card["currency"]:
            return RoundUpDecision(amount, SkipReason.CURRENCY_MISMATCH, deposit, threshold)
        if amount == 0:
            return RoundUpDecision(0, SkipReason.AMOUNT_MULTIPLE_OF_STEP, deposit, threshold)
        if card_balance - amount < threshold:
            return RoundUpDecision(amount, SkipReason.BELOW_SAFETY_THRESHOLD, deposit, threshold)
        return RoundUpDecision(amount, None, deposit, threshold)

    def post(self, conn: sqlite3.Connection, *, user_id: int, card_account_id: int,
             decision: RoundUpDecision, parent_transaction_id: int, now: datetime) -> Tuple[int, int]:
        """Move the round-up from the card to the deposit. Returns (transaction id, new deposit balance).

        The UPDATEs re-check their preconditions, so a concurrent change to either account
        makes the posting fail cleanly instead of breaking the safety threshold.
        """
        amount = decision.amount
        deposit_id = decision.deposit["id"]
        cur = conn.execute(
            """UPDATE card_accounts SET balance = balance - ?
               WHERE id = ? AND status = 'ACTIVE' AND balance - ? >= ?""",
            (amount, card_account_id, amount, decision.safety_balance_threshold),
        )
        if cur.rowcount != 1:
            raise RoundUpPostingError("card balance below the safety threshold or card not active")
        cur = conn.execute(
            "UPDATE deposit_accounts SET balance = balance + ? WHERE id = ? AND status = 'ACTIVE'",
            (amount, deposit_id),
        )
        if cur.rowcount != 1:
            raise RoundUpPostingError("target deposit is not active")

        txn_id = post_transaction(
            conn,
            type="SAVINGS_ROUND_UP_ME_TO_ME",
            status="COMPLETED",
            user_id=user_id,
            amount=amount,
            parent_transaction_id=parent_transaction_id,
            entries=[("CARD", card_account_id, "DEBIT", amount), ("DEPOSIT", deposit_id, "CREDIT", amount)],
            now=now,
        )
        balance = conn.execute("SELECT balance FROM deposit_accounts WHERE id = ?", (deposit_id,)).fetchone()[0]
        return txn_id, balance


class CardPaymentService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow,
                 engine: Optional[RoundUpEngine] = None):
        self._conn = conn
        self._clock = clock
        self._engine = engine or RoundUpEngine()

    def process_card_payment(self, req: PaymentRequest) -> PaymentResult:
        if not isinstance(req.amount, int) or req.amount <= 0:
            raise ValueError("payment amount must be a positive int in tiyin")
        now = self._clock()
        try:
            with transaction(self._conn) as conn:
                replay = self._replay(conn, req)
                if replay is not None:
                    return replay
                return self._process(conn, req, now)
        except PaymentDeclined as exc:
            with transaction(self._conn) as conn:
                write_audit(conn, "CARD_PAYMENT_DECLINED", "card_account", req.card_account_id,
                            {"reason": exc.reason, "amount": req.amount, "merchant": req.merchant_name,
                             "idempotency_key": req.idempotency_key}, now)
            raise

    # -- internals ---------------------------------------------------------------------

    def _process(self, conn: sqlite3.Connection, req: PaymentRequest, now: datetime) -> PaymentResult:
        card = conn.execute(
            "SELECT * FROM card_accounts WHERE id = ? AND user_id = ?", (req.card_account_id, req.user_id)
        ).fetchone()
        if card is None:
            raise PaymentDeclined("CARD_NOT_FOUND")
        if card["status"] != "ACTIVE":
            raise PaymentDeclined("CARD_BLOCKED")
        if card["balance"] < req.amount:
            raise PaymentDeclined("INSUFFICIENT_FUNDS")

        conn.execute("UPDATE card_accounts SET balance = balance - ? WHERE id = ?", (req.amount, card["id"]))
        payment_id = post_transaction(
            conn,
            type="CARD_PAYMENT_ACQUIRING",
            status="COMPLETED",
            user_id=req.user_id,
            amount=req.amount,
            idempotency_key=req.idempotency_key,
            merchant_name=req.merchant_name,
            entries=[("CARD", card["id"], "DEBIT", req.amount),
                     ("MERCHANT_SETTLEMENT", req.merchant_name, "CREDIT", req.amount)],
            now=now,
        )
        balance_after_payment = card["balance"] - req.amount
        decision = self._engine.evaluate(conn, req.user_id, card, balance_after_payment, req.amount)
        result = self._apply_round_up(conn, req, payment_id, balance_after_payment, decision, now)
        self._enqueue_push(conn, req, result, decision, now)
        return result

    def _apply_round_up(self, conn: sqlite3.Connection, req: PaymentRequest, payment_id: int,
                        balance_after_payment: int, decision: RoundUpDecision, now: datetime) -> PaymentResult:
        if not decision.eligible:
            write_audit(conn, "ROUND_UP_SKIPPED", "transaction", payment_id, {
                "reason": decision.skip_reason.value,
                "round_up_amount": decision.amount,
                "card_balance_after_payment": balance_after_payment,
                "safety_balance_threshold": decision.safety_balance_threshold,
            }, now)
            return PaymentResult(payment_id, req.amount, RoundUpStatus.SKIPPED,
                                 round_up_amount=decision.amount, skip_reason=decision.skip_reason)

        try:
            with savepoint(conn, "round_up"):
                round_up_id, deposit_balance = self._engine.post(
                    conn, user_id=req.user_id, card_account_id=req.card_account_id,
                    decision=decision, parent_transaction_id=payment_id, now=now)
        except Exception as exc:  # the purchase must survive any round-up failure
            log.warning("round-up for payment %s failed, scheduling retry: %r", payment_id, exc)
            failed_id = post_transaction(
                conn, type="SAVINGS_ROUND_UP_ME_TO_ME", status="FAILED", user_id=req.user_id,
                amount=decision.amount, parent_transaction_id=payment_id,
                failure_reason=repr(exc)[:500], now=now)
            enqueue_outbox(conn, "ROUND_UP_RETRY", payment_id,
                           {"payment_transaction_id": payment_id, "failed_transaction_id": failed_id}, now)
            write_audit(conn, "ROUND_UP_FAILED", "transaction", payment_id,
                        {"round_up_amount": decision.amount, "error": repr(exc)[:500]}, now)
            return PaymentResult(payment_id, req.amount, RoundUpStatus.FAILED, round_up_amount=decision.amount)

        write_audit(conn, "ROUND_UP_COMPLETED", "transaction", round_up_id, {
            "payment_transaction_id": payment_id,
            "round_up_amount": decision.amount,
            "deposit_account_id": decision.deposit["id"],
        }, now)
        return PaymentResult(payment_id, req.amount, RoundUpStatus.COMPLETED,
                             round_up_amount=decision.amount, round_up_transaction_id=round_up_id,
                             deposit_balance=deposit_balance)

    def _enqueue_push(self, conn: sqlite3.Connection, req: PaymentRequest, result: PaymentResult,
                      decision: RoundUpDecision, now: datetime) -> None:
        completed = result.round_up_status is RoundUpStatus.COMPLETED
        text = payment_push_text(
            req.amount,
            round_up_amount=result.round_up_amount if completed else 0,
            interest_rate_annual=decision.deposit["interest_rate_annual"] if completed else None,
            deposit_balance=result.deposit_balance,
            safety_threshold_skip=(decision.safety_balance_threshold
                                   if result.skip_reason is SkipReason.BELOW_SAFETY_THRESHOLD else None),
        )
        enqueue_outbox(conn, "PUSH_NOTIFICATION", result.payment_transaction_id,
                       {"user_id": req.user_id, "text": text, "payment_transaction_id": result.payment_transaction_id},
                       now)

    def _replay(self, conn: sqlite3.Connection, req: PaymentRequest) -> Optional[PaymentResult]:
        payment = conn.execute(
            "SELECT * FROM transactions WHERE idempotency_key = ?", (req.idempotency_key,)
        ).fetchone()
        if payment is None:
            return None
        if (payment["user_id"], payment["amount"], payment["merchant_name"]) != (
                req.user_id, req.amount, req.merchant_name):
            raise IdempotencyConflict(f"idempotency key {req.idempotency_key!r} reused for another payment")

        child = conn.execute(
            """SELECT * FROM transactions WHERE parent_transaction_id = ? AND type = 'SAVINGS_ROUND_UP_ME_TO_ME'
               ORDER BY status = 'COMPLETED' DESC, id DESC LIMIT 1""",
            (payment["id"],),
        ).fetchone()
        if child is not None and child["status"] == "COMPLETED":
            return PaymentResult(payment["id"], payment["amount"], RoundUpStatus.COMPLETED,
                                 round_up_amount=child["amount"], round_up_transaction_id=child["id"],
                                 replayed=True)
        if child is not None:
            return PaymentResult(payment["id"], payment["amount"], RoundUpStatus.FAILED,
                                 round_up_amount=child["amount"], replayed=True)
        skipped = conn.execute(
            """SELECT payload FROM audit_log WHERE event = 'ROUND_UP_SKIPPED' AND entity_type = 'transaction'
               AND entity_id = ? ORDER BY id DESC LIMIT 1""",
            (payment["id"],),
        ).fetchone()
        payload = json.loads(skipped["payload"]) if skipped else {}
        reason = payload.get("reason")
        return PaymentResult(payment["id"], payment["amount"], RoundUpStatus.SKIPPED,
                             round_up_amount=payload.get("round_up_amount", 0),
                             skip_reason=SkipReason(reason) if reason else None, replayed=True)
