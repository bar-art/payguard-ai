from pathlib import Path
from unittest.mock import patch
import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from app.config import Settings
from app.main import create_app
from app.paypal_client import SimulatedPayPal

DASHBOARD = Path(__file__).resolve().parents[1] / 'dashboard' / 'streamlit_app.py'


@pytest.fixture
def backend(tmp_path):
    settings = Settings(_env_file=None, database_url=f'sqlite:///{tmp_path / "d.db"}')
    with TestClient(create_app(settings, gateway=SimulatedPayPal('http://testserver'), use_llm=False)) as client:
        def route(method, url, timeout=None, **kw):
            return client.request(method, url.replace('http://127.0.0.1:8000', ''), **kw)
        with patch('httpx.request', side_effect=route):
            yield client


def test_dashboard_offline_shows_error():
    request = httpx.Request('GET', 'http://127.0.0.1:8000/config')
    with patch('httpx.request', side_effect=httpx.ConnectError('offline', request=request)):
        page = AppTest.from_file(str(DASHBOARD)).run(timeout=15)
    assert not page.exception and 'Backend unavailable' in page.error[0].value


def test_dashboard_mandate_then_agent_purchase(backend):
    page = AppTest.from_file(str(DASHBOARD)).run(timeout=15)
    assert not page.exception
    next(b for b in page.button if b.label == 'Create mandate').click().run(timeout=15)
    assert backend.get('/mandates').json()
    page.run(timeout=15)
    agent = next(b for b in page.button if b.label == 'Run shopping agent')
    agent.click().run(timeout=15)
    assert not page.exception
    purchases = backend.get('/purchases').json()
    assert len(purchases) == 1 and purchases[0]['status'] == 'awaiting_buyer'


def test_dashboard_finishes_paypal_return(backend):
    mid = backend.post('/mandates', json={
        'owner': 'alex', 'instruction': 'Buy headphones under $150.', 'max_per_purchase': '150',
        'budget_total': '300', 'allowed_categories': ['audio'], 'allowed_merchants': ['harbor-audio.example']}).json()['mandate_id']
    p = backend.post('/purchases', json={'mandate_id': mid, 'merchant': 'harbor-audio.example', 'item': 'ANC-200',
                                         'category': 'audio', 'amount': '99.00',
                                         'agent_rationale': 'Matches the instruction.'}).json()
    def get(url, params=None, timeout=None, follow_redirects=False):
        return backend.get(url.replace('http://127.0.0.1:8000', ''), params=params, follow_redirects=follow_redirects)
    page = AppTest.from_file(str(DASHBOARD))
    page.query_params['paypal'] = 'return'
    page.query_params['token'] = p['paypal_order_id']
    with patch('httpx.get', side_effect=get):
        page.run(timeout=15)
    assert not page.exception
    assert backend.get(f"/purchases/{p['purchase_id']}").json()['status'] == 'captured'
    assert any('Back from PayPal' in s.value for s in page.success)


def test_three_case_demo(backend):
    page = AppTest.from_file(str(DASHBOARD)).run(timeout=15)
    next(b for b in page.button if 'demo' in b.label).click().run(timeout=15)
    assert not page.exception
    decisions = [p['decision'] for p in reversed(backend.get('/purchases').json())]
    # The AI reviewer is off in this test. Rules alone let the wrong item through, which is why the reviewer exists.
    assert decisions == ['allow', 'allow', 'block']
