"""Outbox workers: round-up retries and push delivery."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from .db import transaction
from .journal import enqueue_outbox, write_audit
from .notifications import delayed_round_up_push_text
from .payments import Clock, RoundUpEngine, utcnow

log = logging.getLogger(__name__)

PushSender = Callable[[int, str], None]


@dataclass
class WorkerReport:
    processed: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)


class _OutboxWorker:
    event_type = ""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow, max_attempts: int = 5,
                 backoff: timedelta = timedelta(minutes=1), batch_size: int = 100):
        self._conn = conn
        self._clock = clock
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._batch_size = batch_size

    def _pending(self, now: datetime) -> List[sqlite3.Row]:
        return self._conn.execute(
            """SELECT * FROM outbox_events WHERE event_type = ? AND status = 'PENDING' AND available_at <= ?
               ORDER BY id LIMIT ?""",
            (self.event_type, now.isoformat(), self._batch_size),
        ).fetchall()

    def _mark_sent(self, conn: sqlite3.Connection, event_id: int) -> None:
        conn.execute("UPDATE outbox_events SET status = 'SENT' WHERE id = ?", (event_id,))

    def _record_failure(self, event: sqlite3.Row, exc: Exception, now: datetime) -> None:
        attempts = event["attempts"] + 1
        status = "FAILED" if attempts >= self._max_attempts else "PENDING"
        retry_at = now + self._backoff * (2 ** (attempts - 1))
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE outbox_events SET attempts = ?, status = ?, last_error = ?, available_at = ? WHERE id = ?",
                (attempts, status, repr(exc)[:500], retry_at.isoformat(), event["id"]),
            )
            write_audit(conn, f"{self.event_type}_ATTEMPT_FAILED", "outbox_event", event["id"],
                        {"attempts": attempts, "status": status, "error": repr(exc)[:500]}, now)


class RoundUpRetryWorker(_OutboxWorker):
    """Retries round-ups that failed while the purchase itself went through.

    Eligibility is re-evaluated against the current card balance, so a retry never
    takes the card below the safety threshold.
    """
    event_type = "ROUND_UP_RETRY"

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow,
                 engine: Optional[RoundUpEngine] = None, **kwargs):
        super().__init__(conn, clock, **kwargs)
        self._engine = engine or RoundUpEngine()

    def run_once(self) -> WorkerReport:
        report = WorkerReport()
        now = self._clock()
        for event in self._pending(now):
            report.processed += 1
            try:
                outcome = self._process(event, now)
            except Exception as exc:
                log.warning("round-up retry %s failed: %r", event["id"], exc)
                self._record_failure(event, exc, now)
                report.failed += 1
                report.errors.append(repr(exc))
                continue
            if outcome == "completed":
                report.succeeded += 1
            else:
                report.skipped += 1
        return report

    def _process(self, event: sqlite3.Row, now: datetime) -> str:
        payment_id = json.loads(event["payload"])["payment_transaction_id"]
        with transaction(self._conn) as conn:
            payment = conn.execute("SELECT * FROM transactions WHERE id = ?", (payment_id,)).fetchone()
            done = conn.execute(
                """SELECT 1 FROM transactions WHERE parent_transaction_id = ?
                   AND type = 'SAVINGS_ROUND_UP_ME_TO_ME' AND status = 'COMPLETED'""",
                (payment_id,),
            ).fetchone()
            if done:
                self._mark_sent(conn, event["id"])
                return "skipped"

            card_id = conn.execute(
                """SELECT account_ref FROM ledger_entries WHERE transaction_id = ?
                   AND account_type = 'CARD' AND direction = 'DEBIT'""",
                (payment_id,),
            ).fetchone()[0]
            card = conn.execute("SELECT * FROM card_accounts WHERE id = ?", (int(card_id),)).fetchone()
            decision = self._engine.evaluate(conn, payment["user_id"], card, card["balance"], payment["amount"])
            if not decision.eligible:
                write_audit(conn, "ROUND_UP_SKIPPED", "transaction", payment_id, {
                    "reason": decision.skip_reason.value,
                    "round_up_amount": decision.amount,
                    "card_balance": card["balance"],
                    "safety_balance_threshold": decision.safety_balance_threshold,
                    "on_retry": True,
                }, now)
                self._mark_sent(conn, event["id"])
                return "skipped"

            round_up_id, deposit_balance = self._engine.post(
                conn, user_id=payment["user_id"], card_account_id=card["id"], decision=decision,
                parent_transaction_id=payment_id, now=now)
            write_audit(conn, "ROUND_UP_COMPLETED", "transaction", round_up_id, {
                "payment_transaction_id": payment_id,
                "round_up_amount": decision.amount,
                "deposit_account_id": decision.deposit["id"],
                "on_retry": True,
            }, now)
            enqueue_outbox(conn, "PUSH_NOTIFICATION", payment_id, {
                "user_id": payment["user_id"],
                "text": delayed_round_up_push_text(decision.amount, decision.deposit["interest_rate_annual"],
                                                   deposit_balance),
                "payment_transaction_id": payment_id,
            }, now)
            self._mark_sent(conn, event["id"])
            return "completed"


class PushDispatcher(_OutboxWorker):
    """Delivers queued pushes (at-least-once: a crash after sending may resend)."""
    event_type = "PUSH_NOTIFICATION"

    def __init__(self, conn: sqlite3.Connection, sender: PushSender, clock: Clock = utcnow, **kwargs):
        super().__init__(conn, clock, **kwargs)
        self._sender = sender

    def run_once(self) -> WorkerReport:
        report = WorkerReport()
        now = self._clock()
        for event in self._pending(now):
            report.processed += 1
            payload = json.loads(event["payload"])
            try:
                self._sender(payload["user_id"], payload["text"])
            except Exception as exc:
                self._record_failure(event, exc, now)
                report.failed += 1
                report.errors.append(repr(exc))
                continue
            with transaction(self._conn) as conn:
                self._mark_sent(conn, event["id"])
            report.succeeded += 1
        return report
