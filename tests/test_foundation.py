from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.main import create_app
from app.database import Database
from app.audit import AuditRepository
from app.schemas import PaymentOrder, RiskAssessment, HumanReviewDecision


def test_health_initializes_database(tmp_path):
    path = tmp_path / 'nested' / 'test.db'
    settings = Settings(_env_file=None, database_url=f'sqlite:///{path}')
    with TestClient(create_app(settings)) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        assert client.get('/health').status_code == 200
    assert path.exists()


@pytest.mark.parametrize('amount', ['0', '-1', '1.001', 'NaN'])
def test_invalid_amount(amount):
    with pytest.raises(ValidationError):
        PaymentOrder(order_id='TEST', amount=amount)


def test_order_preserves_money_and_rejects_extra_fields():
    assert PaymentOrder(order_id='TEST', amount='12.30').amount == Decimal('12.30')
    with pytest.raises(ValidationError):
        PaymentOrder(order_id='TEST', amount='12.30', currency='usd')
    with pytest.raises(ValidationError):
        PaymentOrder(order_id='TEST', amount='12.30', card_number='not-accepted')


def test_risk_consistency():
    signals = [{'rule_id':'amount', 'description':'Simulated high amount', 'points':75}]
    assert RiskAssessment(order_id='TEST', score=75, level='high', signals=signals).score == 75
    for score, level in [(75, 'low'), (80, 'high')]:
        with pytest.raises(ValidationError):
            RiskAssessment(order_id='TEST', score=score, level=level, signals=signals)


def test_no_live_paypal_configuration():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, paypal_base_url='https://api-m.paypal.com')


def test_audit_persists_and_filters(tmp_path):
    db = Database(f'sqlite:///{tmp_path / "audit.db"}')
    db.initialize()
    repo = AuditRepository(db)
    decision = HumanReviewDecision(order_id="TEST'1", reviewer='analyst', action='hold', reason='Review amount')
    first = repo.record(decision)
    repo.record(decision.model_copy(update={'reason':'Second review'}))
    rows = AuditRepository(Database(f'sqlite:///{tmp_path / "audit.db"}')).for_order("TEST'1")
    assert len(rows) == 2 and rows[0]['id'] == first
    assert rows[0]['action'] == 'hold' and rows[0]['created_at'].endswith('+00:00')
    assert repo.for_order("' OR 1=1 --") == []


def test_review_requires_reason():
    with pytest.raises(ValidationError):
        HumanReviewDecision(order_id='TEST', reviewer='analyst', action='approve', reason='  ')
