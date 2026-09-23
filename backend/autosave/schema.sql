-- Auto-savings (round-up) backend schema.
-- Every money column is stored in tiyin (1 sum = 100 tiyin) as INTEGER, never as float.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS card_accounts (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    pan_last4   TEXT NOT NULL,
    balance     INTEGER NOT NULL CHECK (balance >= 0),
    currency    TEXT NOT NULL DEFAULT 'UZS',
    status      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'BLOCKED'))
);

-- High-yield savings deposit that receives the round-ups.
CREATE TABLE IF NOT EXISTS deposit_accounts (
    id                          INTEGER PRIMARY KEY,
    user_id                     INTEGER NOT NULL REFERENCES users(id),
    account_number              TEXT NOT NULL UNIQUE,
    balance                     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    currency                    TEXT NOT NULL DEFAULT 'UZS' CHECK (currency = 'UZS'),
    status                      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'BLOCKED')),
    interest_rate_annual        TEXT NOT NULL,              -- decimal percent, e.g. '18.00'
    accrued_interest_balance    INTEGER NOT NULL DEFAULT 0 CHECK (accrued_interest_balance >= 0),
    last_interest_accrual_date  TEXT,                       -- ISO date of the last accrued business day
    opened_on                   TEXT NOT NULL,              -- ISO date, first day that earns interest
    CHECK (CAST(interest_rate_annual AS REAL) >= 0 AND CAST(interest_rate_annual AS REAL) <= 100)
);
CREATE INDEX IF NOT EXISTS ix_deposit_accounts_user ON deposit_accounts(user_id);

CREATE TABLE IF NOT EXISTS auto_savings_settings (
    user_id                     INTEGER PRIMARY KEY REFERENCES users(id),
    is_enabled                  INTEGER NOT NULL DEFAULT 0 CHECK (is_enabled IN (0, 1)),
    round_up_step               INTEGER NOT NULL DEFAULT 100000
                                CHECK (round_up_step IN (100000, 500000, 1000000)),  -- 1 000 / 5 000 / 10 000 sum
    target_deposit_account_id   INTEGER REFERENCES deposit_accounts(id),
    safety_balance_threshold    INTEGER NOT NULL DEFAULT 5000000                     -- 50 000 sum
                                CHECK (safety_balance_threshold >= 0),
    updated_at                  TEXT NOT NULL,
    CHECK (is_enabled = 0 OR target_deposit_account_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS transactions (
    id                      INTEGER PRIMARY KEY,
    type                    TEXT NOT NULL CHECK (type IN (
                                'CARD_PAYMENT_ACQUIRING',
                                'SAVINGS_ROUND_UP_ME_TO_ME',
                                'INTEREST_CAPITALIZATION')),
    status                  TEXT NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    amount                  INTEGER NOT NULL CHECK (amount > 0),
    currency                TEXT NOT NULL DEFAULT 'UZS',
    parent_transaction_id   INTEGER REFERENCES transactions(id),
    idempotency_key         TEXT UNIQUE,
    merchant_name           TEXT,
    failure_reason          TEXT,
    created_at              TEXT NOT NULL,
    CHECK (type <> 'SAVINGS_ROUND_UP_ME_TO_ME' OR parent_transaction_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_transactions_parent ON transactions(parent_transaction_id);
-- A purchase can produce at most one successful round-up, no matter how many retries run.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_completed_round_up_per_payment
    ON transactions(parent_transaction_id)
    WHERE type = 'SAVINGS_ROUND_UP_ME_TO_ME' AND status = 'COMPLETED';

-- Double-entry postings: for every transaction, sum(DEBIT) = sum(CREDIT).
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id),
    account_type    TEXT NOT NULL CHECK (account_type IN (
                        'CARD', 'DEPOSIT', 'MERCHANT_SETTLEMENT', 'BANK_INTEREST_EXPENSE')),
    account_ref     TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
    amount          INTEGER NOT NULL CHECK (amount > 0),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_txn ON ledger_entries(transaction_id);

CREATE TABLE IF NOT EXISTS interest_accruals (
    id                      INTEGER PRIMARY KEY,
    deposit_account_id      INTEGER NOT NULL REFERENCES deposit_accounts(id),
    accrual_date            TEXT NOT NULL,
    balance_snapshot        INTEGER NOT NULL,
    interest_rate_annual    TEXT NOT NULL,
    amount                  INTEGER NOT NULL CHECK (amount >= 0),
    created_at              TEXT NOT NULL,
    UNIQUE (deposit_account_id, accrual_date)
);

-- Transactional outbox: rows are written in the same DB transaction as the business change
-- and delivered afterwards by workers (push notifications, round-up retries).
CREATE TABLE IF NOT EXISTS outbox_events (
    id              INTEGER PRIMARY KEY,
    event_type      TEXT NOT NULL CHECK (event_type IN ('PUSH_NOTIFICATION', 'ROUND_UP_RETRY')),
    aggregate_id    INTEGER,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    available_at    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox_events(status, event_type, available_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    event       TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_log_entity ON audit_log(entity_type, entity_id);
