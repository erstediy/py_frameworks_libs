import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY, customer TEXT NOT NULL, amount TEXT NOT NULL,
    interval_days INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    payment_method_id TEXT, next_charge_at INTEGER, retry_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, subscription_id TEXT REFERENCES subscriptions(id),
    customer TEXT NOT NULL, amount TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'creating', gateway_id TEXT UNIQUE,
    idempotence_key TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL,
    confirmation_url TEXT, reason TEXT, created_at INTEGER NOT NULL, completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY, payment_id TEXT NOT NULL REFERENCES payments(id),
    amount TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'creating', gateway_id TEXT UNIQUE,
    idempotence_key TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL,
    reason TEXT, created_at INTEGER NOT NULL, completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL, sent_at INTEGER, last_error TEXT
);
"""


class Storage:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()
