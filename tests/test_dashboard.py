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
    page.button[0].click().run(timeout=15)            # "Create mandate" form submit
    assert backend.get('/mandates').json()
    page.run(timeout=15)
    agent = next(b for b in page.button if b.label == 'Run shopping agent')
    agent.click().run(timeout=15)
    assert not page.exception
    purchases = backend.get('/purchases').json()
    assert len(purchases) == 1 and purchases[0]['status'] == 'awaiting_buyer'
