"""Writes to the ledger, the audit log and the outbox. Callers own the DB transaction."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

# (account_type, account_ref, direction, amount)
Entry = Tuple[str, str, str, int]


def post_transaction(
    conn: sqlite3.Connection,
    *,
    type: str,
    status: str,
    user_id: int,
    amount: int,
    now: datetime,
    entries: Iterable[Entry] = (),
    parent_transaction_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    merchant_name: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> int:
    entries = list(entries)
    debit = sum(e[3] for e in entries if e[2] == "DEBIT")
    credit = sum(e[3] for e in entries if e[2] == "CREDIT")
    if debit != credit:
        raise ValueError(f"unbalanced posting: debit {debit} != credit {credit}")
    if status == "COMPLETED" and debit != amount:
        raise ValueError("completed transaction must post exactly its amount")

    ts = now.isoformat()
    txn_id = conn.execute(
        """INSERT INTO transactions (type, status, user_id, amount, parent_transaction_id,
                                     idempotency_key, merchant_name, failure_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (type, status, user_id, amount, parent_transaction_id, idempotency_key,
         merchant_name, failure_reason, ts),
    ).lastrowid
    conn.executemany(
        """INSERT INTO ledger_entries (transaction_id, account_type, account_ref, direction, amount, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(txn_id, acc_type, str(ref), direction, value, ts) for acc_type, ref, direction, value in entries],
    )
    return txn_id


def write_audit(
    conn: sqlite3.Connection,
    event: str,
    entity_type: str,
    entity_id: Optional[int],
    payload: Dict[str, Any],
    now: datetime,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (occurred_at, event, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)",
        (now.isoformat(), event, entity_type, entity_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )


def enqueue_outbox(
    conn: sqlite3.Connection,
    event_type: str,
    aggregate_id: Optional[int],
    payload: Dict[str, Any],
    now: datetime,
) -> int:
    ts = now.isoformat()
    return conn.execute(
        """INSERT INTO outbox_events (event_type, aggregate_id, payload, available_at, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (event_type, aggregate_id, json.dumps(payload, ensure_ascii=False), ts, ts),
    ).lastrowid
