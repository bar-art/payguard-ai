import sqlite3
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator


class Database:
    def __init__(self, database_url: str):
        if not database_url.startswith('sqlite:///'):
            raise ValueError('Only SQLite is supported')
        self.path = Path(database_url.removeprefix('sqlite:///'))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('approve','hold','reject')),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_order_idx ON audit_log(order_id);

                CREATE TABLE IF NOT EXISTS mandates (
                    mandate_id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS purchases (
                    purchase_id TEXT PRIMARY KEY,
                    mandate_id TEXT NOT NULL REFERENCES mandates(mandate_id),
                    intent TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    guard TEXT NOT NULL,
                    paypal_order_id TEXT UNIQUE,
                    approve_url TEXT,
                    authorization_id TEXT,
                    capture_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS purchases_mandate_idx ON purchases(mandate_id);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchase_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    prev_hash TEXT,
                    hash TEXT
                );
                CREATE INDEX IF NOT EXISTS events_purchase_idx ON events(purchase_id);
            ''')
            cols = {r[1] for r in conn.execute('PRAGMA table_info(events)')}
            for col in [c for c in ('prev_hash', 'hash') if c not in cols]:
                conn.execute(f'ALTER TABLE events ADD COLUMN {col} TEXT')
            from app.audit import backfill_chain
            backfill_chain(conn)
