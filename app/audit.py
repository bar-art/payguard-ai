import hashlib
import json
from datetime import datetime, timezone
from app.database import Database
from app.schemas import HumanReviewDecision


class AuditRepository:
    """Local application audit trail; not a tamper-proof compliance archive."""
    def __init__(self, database: Database):
        self.database = database

    def record(self, decision: HumanReviewDecision) -> int:
        with self.database.connect() as conn:
            cursor = conn.execute(
                'INSERT INTO audit_log(order_id,reviewer,action,reason,created_at) VALUES (?,?,?,?,?)',
                (decision.order_id, decision.reviewer, decision.action.value,
                 decision.reason, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid)

    def for_order(self, order_id: str) -> list[dict]:
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM audit_log WHERE order_id=? ORDER BY id', (order_id,)
            )]


GENESIS = '0' * 64


def event_hash(prev_hash: str, purchase_id: str, actor: str, event: str, detail: str, created_at: str) -> str:
    """SHA-256 over the previous hash and this event, so editing any past event breaks every later link."""
    body = json.dumps([prev_hash, purchase_id, actor, event, detail, created_at], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def append_event(conn, purchase_id: str, actor: str, event: str, detail: str, created_at: str) -> str:
    """Append to the hash chain. Call inside the writer's transaction so two writers can't share a parent."""
    last = conn.execute('SELECT hash FROM events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
    prev = last[0] if last else GENESIS
    digest = event_hash(prev, purchase_id, actor, event, detail, created_at)
    conn.execute('INSERT INTO events(purchase_id, actor, event, detail, created_at, prev_hash, hash) VALUES (?,?,?,?,?,?,?)',
                 (purchase_id, actor, event, detail, created_at, prev, digest))
    return digest


def verify_chain(conn) -> dict:
    """Recompute every link. Detects edited, deleted or reordered events (not a wholesale rewrite of the
    whole chain: for that, publish the head hash somewhere the operator can't change)."""
    prev = GENESIS
    count = 0
    for row in conn.execute('SELECT id, purchase_id, actor, event, detail, created_at, prev_hash, hash FROM events ORDER BY id'):
        count += 1
        expected = event_hash(prev, row['purchase_id'], row['actor'], row['event'], row['detail'], row['created_at'])
        if row['prev_hash'] != prev or row['hash'] != expected:
            return {'ok': False, 'events': count, 'first_bad_event_id': row['id'], 'head_hash': None}
        prev = row['hash']
    return {'ok': True, 'events': count, 'first_bad_event_id': None, 'head_hash': prev}


def backfill_chain(conn) -> None:
    """Chain events written before hashing existed (only if none are hashed yet)."""
    if conn.execute('SELECT COUNT(*) FROM events WHERE hash IS NOT NULL').fetchone()[0]:
        return
    prev = GENESIS
    for row in conn.execute('SELECT id, purchase_id, actor, event, detail, created_at FROM events ORDER BY id').fetchall():
        digest = event_hash(prev, row['purchase_id'], row['actor'], row['event'], row['detail'], row['created_at'])
        conn.execute('UPDATE events SET prev_hash=?, hash=? WHERE id=?', (prev, digest, row['id']))
        prev = digest
