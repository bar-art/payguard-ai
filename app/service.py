"""The purchase lifecycle.

submit -> guard (rules + AI review) -> BLOCK: no PayPal order
                                    -> ALLOW / REVIEW: PayPal order (intent AUTHORIZE), buyer approves
buyer approval -> authorize (funds held) -> ALLOW: capture
                                        -> REVIEW: human captures or voids
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.ai import AIUnavailable, MandateReviewer
from app.audit import AuditRepository
from app.database import Database
from app.paypal_client import PaymentGateway, PayPalError
from app.risk_engine import History, check_rules, combine, recent_cutoff
from app.schemas import (Decision, GuardResult, HumanReviewDecision, Mandate, PurchaseIntent, PurchaseStatus,
                         ReviewAction, ReviewRequest, StoredMandate)

class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GuardService:
    def __init__(self, db: Database, gateway: PaymentGateway, reviewer: MandateReviewer | None,
                 backend_url: str, dashboard_url: str):
        self.db = db
        self.gateway = gateway
        self.reviewer = reviewer
        self.backend_url = backend_url.rstrip('/')
        self.dashboard_url = dashboard_url.rstrip('/')
        self.audit = AuditRepository(db)
        self._lock = threading.Lock()  # one state change at a time; SQLite is the source of truth

    # ---- mandates
    def create_mandate(self, mandate: Mandate) -> StoredMandate:
        stored = StoredMandate(**mandate.model_dump(), mandate_id='m_' + uuid.uuid4().hex[:10],
                               created_at=datetime.now(timezone.utc))
        with self.db.connect() as conn:
            conn.execute('INSERT INTO mandates(mandate_id, body, created_at) VALUES (?,?,?)',
                         (stored.mandate_id, stored.model_dump_json(), stored.created_at.isoformat()))
        return stored

    def get_mandate(self, mandate_id: str) -> StoredMandate:
        with self.db.connect() as conn:
            row = conn.execute('SELECT body FROM mandates WHERE mandate_id=?', (mandate_id,)).fetchone()
        if row is None:
            raise NotFound(f'mandate {mandate_id} not found')
        return StoredMandate.model_validate_json(row['body'])

    def list_mandates(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute('SELECT mandate_id FROM mandates ORDER BY created_at DESC').fetchall()
        out = []
        for row in rows:
            m = self.get_mandate(row['mandate_id'])
            out.append({**m.model_dump(mode='json'), 'committed': str(self._history(m.mandate_id).committed)})
        return out

    # ---- purchases
    def _history(self, mandate_id: str) -> History:
        with self.db.connect() as conn:
            # An unapproved PayPal order expires after about 3 hours, so it stops counting then.
            rows = conn.execute('''SELECT amount FROM purchases WHERE mandate_id=? AND
                                   (status IN (?, ?) OR (status=? AND created_at>=?))''',
                                (mandate_id, PurchaseStatus.IN_REVIEW.value, PurchaseStatus.CAPTURED.value,
                                 PurchaseStatus.AWAITING_BUYER.value,
                                 (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())).fetchall()
            recent = conn.execute('SELECT COUNT(*) FROM purchases WHERE mandate_id=? AND created_at>=?',
                                  (mandate_id, recent_cutoff().isoformat())).fetchone()[0]
        return History(committed=sum((Decimal(r['amount']) for r in rows), Decimal('0')), recent_count=recent)

    def _event(self, conn, purchase_id: str, actor: str, event: str, **detail) -> None:
        conn.execute('INSERT INTO events(purchase_id, actor, event, detail, created_at) VALUES (?,?,?,?,?)',
                     (purchase_id, actor, event, json.dumps(detail, default=str), _now()))

    def _set(self, conn, purchase_id: str, **fields) -> None:
        fields['updated_at'] = _now()
        cols = ', '.join(f'{k}=?' for k in fields)
        conn.execute(f'UPDATE purchases SET {cols} WHERE purchase_id=?', (*fields.values(), purchase_id))

    def guard(self, purchase_id: str, mandate: StoredMandate, intent: PurchaseIntent) -> GuardResult:
        signals, hard = check_rules(mandate, intent, self._history(mandate.mandate_id))
        if self.reviewer is None:
            return combine(purchase_id, signals, hard, None, 'disabled')
        try:
            ai = self.reviewer.review(mandate, intent, signals, hard)
            return combine(purchase_id, signals, hard, ai, 'ok', self.reviewer.llm.model)
        except AIUnavailable:
            return combine(purchase_id, signals, hard, None, 'unavailable', self.reviewer.llm.model)

    def submit(self, intent: PurchaseIntent, origin: str = 'api') -> dict:
        mandate = self.get_mandate(intent.mandate_id)
        purchase_id = 'p_' + uuid.uuid4().hex[:12]
        with self._lock:
            result = self.guard(purchase_id, mandate, intent)
            with self.db.connect() as conn:
                conn.execute('''INSERT INTO purchases(purchase_id, mandate_id, intent, amount, status, decision, score,
                                guard, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                             (purchase_id, mandate.mandate_id, intent.model_dump_json(), str(intent.amount),
                              PurchaseStatus.BLOCKED.value if result.decision == Decision.BLOCK else 'pending',
                              result.decision.value, result.assessment.score, result.model_dump_json(), _now(), _now()))
                self._event(conn, purchase_id, intent.agent_id, 'intent_submitted', origin=origin,
                            merchant=intent.merchant, item=intent.item, amount=str(intent.amount))
                self._event(conn, purchase_id, 'payguard', 'guard_decision', decision=result.decision.value,
                            score=result.assessment.score, hard_blocks=result.hard_blocks, ai_status=result.ai_status)
            if result.decision != Decision.BLOCK:
                self._create_order(purchase_id, intent)
        return self.get_purchase(purchase_id)

    def _create_order(self, purchase_id: str, intent: PurchaseIntent) -> None:
        try:
            order = self.gateway.create_order(
                reference_id=purchase_id, amount=intent.amount, currency=intent.currency,
                description=f'{intent.item} ({intent.merchant})',
                return_url=f'{self.backend_url}/paypal/return', cancel_url=f'{self.backend_url}/paypal/cancel')
        except PayPalError as exc:
            with self.db.connect() as conn:
                self._set(conn, purchase_id, status=PurchaseStatus.ERROR.value)
                self._event(conn, purchase_id, 'paypal', 'order_failed', error=str(exc), debug_id=exc.debug_id)
            return
        with self.db.connect() as conn:
            self._set(conn, purchase_id, status=PurchaseStatus.AWAITING_BUYER.value,
                      paypal_order_id=order['id'], approve_url=order['approve_url'])
            self._event(conn, purchase_id, 'paypal', 'order_created', paypal_order_id=order['id'],
                        intent='AUTHORIZE', status=order['status'])

    def _purchase_row(self, conn, purchase_id: str | None = None, order_id: str | None = None):
        if purchase_id:
            row = conn.execute('SELECT * FROM purchases WHERE purchase_id=?', (purchase_id,)).fetchone()
        else:
            row = conn.execute('SELECT * FROM purchases WHERE paypal_order_id=?', (order_id,)).fetchone()
        if row is None:
            raise NotFound('purchase not found')
        return row

    def buyer_approved(self, paypal_order_id: str) -> dict:
        """PayPal redirected the buyer back. Authorize, then capture or queue for review."""
        with self._lock:
            with self.db.connect() as conn:
                row = self._purchase_row(conn, order_id=paypal_order_id)
            pid = row['purchase_id']
            if row['status'] != PurchaseStatus.AWAITING_BUYER.value:
                return self.get_purchase(pid)  # a repeated redirect changes nothing
            try:
                auth = self.gateway.authorize_order(paypal_order_id, request_id=f'{pid}-authorize')
            except PayPalError as exc:
                with self.db.connect() as conn:
                    self._set(conn, pid, status=PurchaseStatus.ERROR.value)
                    self._event(conn, pid, 'paypal', 'authorize_failed', error=str(exc), debug_id=exc.debug_id)
                return self.get_purchase(pid)
            with self.db.connect() as conn:
                self._set(conn, pid, status=PurchaseStatus.IN_REVIEW.value, authorization_id=auth['authorization_id'])
                self._event(conn, pid, 'paypal', 'authorized', authorization_id=auth['authorization_id'],
                            status=auth['status'], expires=auth.get('expiration_time'))
            if row['decision'] == Decision.ALLOW.value:
                self._capture(pid, auth['authorization_id'], actor='payguard')
            else:
                with self.db.connect() as conn:
                    self._event(conn, pid, 'payguard', 'held_for_review', note='funds authorized, not captured')
        return self.get_purchase(pid)

    def buyer_cancelled(self, paypal_order_id: str) -> dict:
        with self._lock, self.db.connect() as conn:
            row = self._purchase_row(conn, order_id=paypal_order_id)
            if row['status'] == PurchaseStatus.AWAITING_BUYER.value:
                self._set(conn, row['purchase_id'], status=PurchaseStatus.CANCELLED.value)
                self._event(conn, row['purchase_id'], 'buyer', 'cancelled_at_paypal')
        return self.get_purchase(row['purchase_id'])

    def _capture(self, pid: str, authorization_id: str, actor: str) -> None:
        try:
            cap = self.gateway.capture_authorization(authorization_id, request_id=f'{pid}-capture')
        except PayPalError as exc:
            with self.db.connect() as conn:  # stays in review: the hold is still there and a human can retry
                self._set(conn, pid, status=PurchaseStatus.IN_REVIEW.value)
                self._event(conn, pid, 'paypal', 'capture_failed', error=str(exc), debug_id=exc.debug_id)
            return
        with self.db.connect() as conn:
            self._set(conn, pid, status=PurchaseStatus.CAPTURED.value, capture_id=cap['capture_id'])
            self._event(conn, pid, actor, 'captured', capture_id=cap['capture_id'], status=cap['status'])

    def review(self, purchase_id: str, req: ReviewRequest) -> dict:
        if req.action == ReviewAction.HOLD:
            raise Conflict('hold is the current state; choose approve or reject')
        with self._lock:
            with self.db.connect() as conn:
                row = self._purchase_row(conn, purchase_id=purchase_id)
            if row['status'] != PurchaseStatus.IN_REVIEW.value:
                raise Conflict(f"purchase is {row['status']}, not in review")
            self.audit.record(HumanReviewDecision(order_id=purchase_id, reviewer=req.reviewer,
                                                  action=req.action, reason=req.reason))
            with self.db.connect() as conn:
                self._event(conn, purchase_id, f'human:{req.reviewer}', f'review_{req.action.value}', reason=req.reason)
            if req.action == ReviewAction.APPROVE:
                self._capture(purchase_id, row['authorization_id'], actor=f'human:{req.reviewer}')
            else:
                try:
                    self.gateway.void_authorization(row['authorization_id'], request_id=f'{purchase_id}-void')
                except PayPalError as exc:
                    with self.db.connect() as conn:
                        self._event(conn, purchase_id, 'paypal', 'void_failed', error=str(exc), debug_id=exc.debug_id)
                    return self.get_purchase(purchase_id)
                with self.db.connect() as conn:
                    self._set(conn, purchase_id, status=PurchaseStatus.VOIDED.value)
                    self._event(conn, purchase_id, 'paypal', 'voided', authorization_id=row['authorization_id'])
        return self.get_purchase(purchase_id)

    # ---- reads
    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        d['intent'] = json.loads(d['intent'])
        d['guard'] = json.loads(d['guard'])
        return d

    def _events(self, conn, purchase_id: str) -> list[dict]:
        return [dict(e, detail=json.loads(e['detail'])) for e in conn.execute(
            'SELECT actor, event, detail, created_at FROM events WHERE purchase_id=? ORDER BY id', (purchase_id,))]

    def get_purchase(self, purchase_id: str) -> dict:
        with self.db.connect() as conn:
            d = self._row_to_dict(self._purchase_row(conn, purchase_id=purchase_id))
            d['events'] = self._events(conn, purchase_id)
        return d

    def list_purchases(self, status: str | None = None) -> list[dict]:
        with self.db.connect() as conn:
            if status:
                rows = conn.execute('SELECT * FROM purchases WHERE status=? ORDER BY created_at DESC', (status,))
            else:
                rows = conn.execute('SELECT * FROM purchases ORDER BY created_at DESC')
            out = [self._row_to_dict(r) for r in rows.fetchall()]
            for d in out:
                d['events'] = self._events(conn, d['purchase_id'])
            return out
