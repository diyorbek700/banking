from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from autosave.db import connect, migrate
from autosave.money import sum_

# Round-up failures are expected in some tests; keep their warnings out of the test output.
logging.getLogger("autosave").setLevel(logging.ERROR)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def fixed_clock():
    return NOW


@dataclass
class Env:
    conn: object
    user_id: int = 1
    card_id: int = 1
    deposit_id: int = 1

    def card_balance(self) -> int:
        return self.conn.execute("SELECT balance FROM card_accounts WHERE id = ?", (self.card_id,)).fetchone()[0]

    def deposit(self):
        return self.conn.execute("SELECT * FROM deposit_accounts WHERE id = ?", (self.deposit_id,)).fetchone()

    def transactions(self, type_: str = None) -> List:
        sql, args = "SELECT * FROM transactions", ()
        if type_:
            sql, args = sql + " WHERE type = ?", (type_,)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    def outbox(self, event_type: str, status: str = None) -> List[dict]:
        sql, args = "SELECT * FROM outbox_events WHERE event_type = ?", [event_type]
        if status:
            sql += " AND status = ?"
            args.append(status)
        return [{**dict(row), "payload": json.loads(row["payload"])}
                for row in self.conn.execute(sql + " ORDER BY id", args)]

    def audit(self, event: str) -> List[dict]:
        return [json.loads(r["payload"]) for r in
                self.conn.execute("SELECT payload FROM audit_log WHERE event = ? ORDER BY id", (event,))]

    def assert_ledger_balanced(self, test) -> None:
        rows = self.conn.execute(
            """SELECT transaction_id,
                      SUM(CASE WHEN direction = 'DEBIT' THEN amount ELSE 0 END) AS debit,
                      SUM(CASE WHEN direction = 'CREDIT' THEN amount ELSE 0 END) AS credit
               FROM ledger_entries GROUP BY transaction_id""").fetchall()
        for row in rows:
            test.assertEqual(row["debit"], row["credit"], f"transaction {row['transaction_id']} unbalanced")


def make_env(*, card_balance=300_000, deposit_balance=183_400, step=5000, safety_threshold=50_000,
             rate="18.00", enabled=True, deposit_status="ACTIVE", opened_on="2026-09-01") -> Env:
    """Amounts in sum. Default deposit balance makes the spec example end at 185 000 sum."""
    conn = connect()
    migrate(conn)
    ts = NOW.isoformat()
    conn.execute("INSERT INTO users (id, full_name, created_at) VALUES (1, 'Client', ?)", (ts,))
    conn.execute("INSERT INTO users (id, full_name, created_at) VALUES (2, 'Other client', ?)", (ts,))
    conn.execute("INSERT INTO card_accounts (id, user_id, pan_last4, balance) VALUES (1, 1, '5748', ?)",
                 (sum_(card_balance),))
    conn.execute(
        """INSERT INTO deposit_accounts (id, user_id, account_number, balance, status, interest_rate_annual, opened_on)
           VALUES (1, 1, '20206000491100000001', ?, ?, ?, ?)""",
        (sum_(deposit_balance), deposit_status, rate, opened_on),
    )
    conn.execute(
        """INSERT INTO auto_savings_settings
           (user_id, is_enabled, round_up_step, target_deposit_account_id, safety_balance_threshold, updated_at)
           VALUES (1, ?, ?, 1, ?, ?)""",
        (1 if enabled else 0, sum_(step), sum_(safety_threshold), ts),
    )
    return Env(conn)
