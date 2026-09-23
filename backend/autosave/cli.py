"""Command line entry points.

    python3 -m autosave.cli demo                       # end-to-end scenario on an in-memory DB
    python3 -m autosave.cli init-db savings.db         # create the schema
    python3 -m autosave.cli eod savings.db 2026-09-30  # run the EOD interest job
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from .db import connect, migrate
from .interest import InterestAccrualService
from .money import sum_
from .notifications import format_sum
from .outbox import PushDispatcher
from .payments import CardPaymentService, PaymentRequest


def _seed_demo(conn) -> None:
    now = "2026-09-01T09:00:00+00:00"
    conn.execute("INSERT INTO users (id, full_name, created_at) VALUES (1, 'Demo client', ?)", (now,))
    conn.execute("INSERT INTO card_accounts (id, user_id, pan_last4, balance) VALUES (1, 1, '5748', ?)",
                 (sum_(293711),))
    conn.execute(
        """INSERT INTO deposit_accounts (id, user_id, account_number, balance, interest_rate_annual, opened_on)
           VALUES (1, 1, '20206000491100000001', ?, '18.00', '2026-09-01')""",
        (sum_(183400),),
    )
    conn.execute(
        """INSERT INTO auto_savings_settings
           (user_id, is_enabled, round_up_step, target_deposit_account_id, safety_balance_threshold, updated_at)
           VALUES (1, 1, ?, 1, ?, ?)""",
        (sum_(5000), sum_(50000), now),
    )


def demo() -> None:
    conn = connect()
    migrate(conn)
    _seed_demo(conn)
    clock = lambda: datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)  # noqa: E731
    payments = CardPaymentService(conn, clock)

    for key, merchant, amount in [("demo-1", "Korzinka", 23400), ("demo-2", "Evos", 200000)]:
        result = payments.process_card_payment(PaymentRequest(1, 1, merchant, sum_(amount), key))
        print(f"{merchant}: {format_sum(sum_(amount))} -> round-up {result.round_up_status.value}"
              f" {format_sum(result.round_up_amount)}"
              + (f" ({result.skip_reason.value})" if result.skip_reason else ""))

    PushDispatcher(conn, lambda user_id, text: print(f"  push to user {user_id}: {text}"), clock).run_once()

    report = InterestAccrualService(conn, clock).run_eod(date(2026, 9, 30))
    deposit = conn.execute("SELECT balance, accrued_interest_balance FROM deposit_accounts WHERE id = 1").fetchone()
    print(f"EOD {report.business_date}: {report.days_accrued} days accrued, "
          f"interest {format_sum(report.interest_accrued)}, capitalized {format_sum(report.interest_capitalized)}")
    print(f"Deposit balance: {format_sum(deposit['balance'])}, accrued not yet paid: "
          f"{format_sum(deposit['accrued_interest_balance'])}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="autosave")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    init = sub.add_parser("init-db")
    init.add_argument("path")
    eod = sub.add_parser("eod")
    eod.add_argument("path")
    eod.add_argument("business_date", nargs="?", help="YYYY-MM-DD, defaults to yesterday")
    args = parser.parse_args(argv)

    if args.command == "demo":
        demo()
    elif args.command == "init-db":
        migrate(connect(args.path))
        print(f"schema created in {args.path}")
    elif args.command == "eod":
        day = date.fromisoformat(args.business_date) if args.business_date else date.today() - timedelta(days=1)
        report = InterestAccrualService(connect(args.path)).run_eod(day)
        print(report)
        return 1 if report.errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
