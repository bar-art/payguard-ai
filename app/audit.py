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
