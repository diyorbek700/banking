"""End-of-day interest accrual for savings deposits.

Daily interest = balance × interest_rate_annual / 100 / 365, rounded to the tiyin
(banker's rounding). It accrues daily into `accrued_interest_balance` and is paid out
(capitalized into `balance`) on the last day of each month, so from the next month
interest is earned on the interest.
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import List, Tuple, Union

from .db import transaction
from .journal import post_transaction, write_audit
from .payments import Clock, utcnow

log = logging.getLogger(__name__)

DAYS_IN_YEAR = 365


def daily_interest(balance: int, interest_rate_annual: Union[str, Decimal]) -> int:
    """Interest in tiyin earned in one day on `balance` tiyin."""
    rate = Decimal(interest_rate_annual)
    if balance < 0:
        raise ValueError("balance cannot be negative")
    if not Decimal(0) <= rate <= Decimal(100):
        raise ValueError("annual rate must be a percentage between 0 and 100")
    raw = Decimal(balance) * rate / Decimal(100) / Decimal(DAYS_IN_YEAR)
    return int(raw.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def is_month_end(day: date) -> bool:
    return day.day == calendar.monthrange(day.year, day.month)[1]


@dataclass
class EodReport:
    business_date: date
    accounts_processed: int = 0
    days_accrued: int = 0
    interest_accrued: int = 0
    interest_capitalized: int = 0
    errors: List[Tuple[int, str]] = field(default_factory=list)


class InterestAccrualService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow):
        self._conn = conn
        self._clock = clock

    def run_eod(self, business_date: date) -> EodReport:
        """Accrue interest for every deposit up to and including `business_date`.

        Safe to re-run: days already accrued are skipped, and missed days since the last
        run are caught up one by one. Each account is processed in its own transaction,
        so one failing account does not stop the others.
        """
        report = EodReport(business_date)
        account_ids = [row[0] for row in self._conn.execute("SELECT id FROM deposit_accounts ORDER BY id")]
        for account_id in account_ids:
            now = self._clock()
            try:
                with transaction(self._conn) as conn:
                    days, accrued, capitalized = self._accrue_account(conn, account_id, business_date, now)
            except Exception as exc:
                log.exception("interest accrual failed for deposit %s", account_id)
                report.errors.append((account_id, repr(exc)))
                with transaction(self._conn) as conn:
                    write_audit(conn, "INTEREST_ACCRUAL_FAILED", "deposit_account", account_id,
                                {"business_date": business_date.isoformat(), "error": repr(exc)[:500]}, now)
                continue
            report.accounts_processed += 1
            report.days_accrued += days
            report.interest_accrued += accrued
            report.interest_capitalized += capitalized
        return report

    def _accrue_account(self, conn: sqlite3.Connection, account_id: int, business_date: date,
                        now: datetime) -> Tuple[int, int, int]:
        acc = conn.execute("SELECT * FROM deposit_accounts WHERE id = ?", (account_id,)).fetchone()
        last = acc["last_interest_accrual_date"]
        day = date.fromisoformat(last) + timedelta(days=1) if last else date.fromisoformat(acc["opened_on"])
        if day > business_date:
            return 0, 0, 0

        rate = acc["interest_rate_annual"]
        balance = acc["balance"]
        accrued = acc["accrued_interest_balance"]
        days = total_accrued = total_capitalized = 0

        while day <= business_date:
            interest = daily_interest(balance, rate)
            conn.execute(
                """INSERT INTO interest_accruals
                   (deposit_account_id, accrual_date, balance_snapshot, interest_rate_annual, amount, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, day.isoformat(), balance, rate, interest, now.isoformat()),
            )
            write_audit(conn, "INTEREST_ACCRUED", "deposit_account", account_id, {
                "accrual_date": day.isoformat(),
                "balance": balance,
                "interest_rate_annual": rate,
                "amount": interest,
            }, now)
            accrued += interest
            total_accrued += interest
            days += 1

            if is_month_end(day) and accrued > 0:
                post_transaction(
                    conn,
                    type="INTEREST_CAPITALIZATION",
                    status="COMPLETED",
                    user_id=acc["user_id"],
                    amount=accrued,
                    entries=[("BANK_INTEREST_EXPENSE", "interest-expense-uzs", "DEBIT", accrued),
                             ("DEPOSIT", account_id, "CREDIT", accrued)],
                    now=now,
                )
                write_audit(conn, "INTEREST_CAPITALIZED", "deposit_account", account_id,
                            {"period_end": day.isoformat(), "amount": accrued}, now)
                balance += accrued
                total_capitalized += accrued
                accrued = 0
            day += timedelta(days=1)

        # Balance is changed by delta (not overwritten) so concurrent round-up credits are kept.
        conn.execute(
            """UPDATE deposit_accounts
               SET balance = balance + ?, accrued_interest_balance = ?, last_interest_accrual_date = ?
               WHERE id = ?""",
            (total_capitalized, accrued, business_date.isoformat(), account_id),
        )
        return days, total_accrued, total_capitalized
