"""PayPal Orders v2 with intent AUTHORIZE: money is held at approval and moves only on capture.

That is what lets the guard sit between an agent and the money: a purchase in review is
authorized (funds held), and a human either captures it or voids the authorization.
"""
import time
import uuid
from decimal import Decimal
from typing import Protocol
import httpx


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id


class PaymentGateway(Protocol):
    mode: str

    def create_order(self, *, reference_id: str, amount: Decimal, currency: str, description: str,
                     return_url: str, cancel_url: str) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def authorize_order(self, order_id: str, *, request_id: str) -> dict: ...
    def capture_authorization(self, authorization_id: str, *, request_id: str) -> dict: ...
    def void_authorization(self, authorization_id: str, *, request_id: str) -> None: ...


def _first_authorization(order: dict) -> dict:
    try:
        return order['purchase_units'][0]['payments']['authorizations'][0]
    except (KeyError, IndexError, TypeError):
        raise PayPalError('PayPal response has no authorization') from None


class PayPalSandbox:
    """Thin client for the PayPal sandbox REST API. Secrets never leave this object."""
    mode = 'sandbox'

    def __init__(self, client_id: str, client_secret: str,
                 base_url: str = 'https://api-m.sandbox.paypal.com', http: httpx.Client | None = None):
        if 'sandbox' not in base_url:
            raise ValueError('PayGuard only talks to the PayPal sandbox')
        self._auth = (client_id, client_secret)
        self._base = base_url.rstrip('/')
        self._http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_expiry = 0.0

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry - 60:
            return self._token
        r = self._http.post(f'{self._base}/v1/oauth2/token', auth=self._auth,
                            data={'grant_type': 'client_credentials'},
                            headers={'Accept': 'application/json'})
        if r.status_code != 200:
            raise PayPalError('PayPal OAuth failed; check the sandbox client id and secret', r.status_code)
        body = r.json()
        self._token = body['access_token']
        self._token_expiry = time.monotonic() + float(body.get('expires_in', 300))
        return self._token

    def _request(self, method: str, path: str, *, json: dict | None = None,
                 request_id: str | None = None) -> httpx.Response:
        headers = {'Authorization': f'Bearer {self._access_token()}', 'Content-Type': 'application/json'}
        if request_id:
            headers['PayPal-Request-Id'] = request_id  # makes retries idempotent
        r = self._http.request(method, f'{self._base}{path}', json=json, headers=headers)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            detail = body.get('details', [{}])[0].get('issue') if body.get('details') else None
            raise PayPalError(f"PayPal {body.get('name', 'error')}: {detail or body.get('message', r.text[:200])}",
                              r.status_code, body.get('debug_id'))
        return r

    def create_order(self, *, reference_id, amount, currency, description, return_url, cancel_url) -> dict:
        payload = {
            'intent': 'AUTHORIZE',
            'purchase_units': [{
                'reference_id': reference_id,
                'custom_id': reference_id,
                'description': description[:127],
                'amount': {'currency_code': currency, 'value': f'{Decimal(amount):.2f}'},
            }],
            'payment_source': {'paypal': {'experience_context': {
                'brand_name': 'PayGuard demo',
                'shipping_preference': 'NO_SHIPPING',
                'user_action': 'CONTINUE',  # the payment completes after the guard, not on this page
                'return_url': return_url,
                'cancel_url': cancel_url,
            }}},
        }
        order = self._request('POST', '/v2/checkout/orders', json=payload,
                              request_id=f'{reference_id}-create').json()
        approve = next((l['href'] for l in order.get('links', []) if l.get('rel') in ('payer-action', 'approve')), None)
        if not approve:
            raise PayPalError('PayPal order has no approval link')
        return {'id': order['id'], 'status': order['status'], 'approve_url': approve}

    def get_order(self, order_id: str) -> dict:
        return self._request('GET', f'/v2/checkout/orders/{order_id}').json()

    def authorize_order(self, order_id: str, *, request_id: str) -> dict:
        order = self._request('POST', f'/v2/checkout/orders/{order_id}/authorize', json={},
                              request_id=request_id).json()
        auth = _first_authorization(order)
        return {'order_status': order['status'], 'authorization_id': auth['id'], 'status': auth['status'],
                'expiration_time': auth.get('expiration_time')}

    def capture_authorization(self, authorization_id: str, *, request_id: str) -> dict:
        body = self._request('POST', f'/v2/payments/authorizations/{authorization_id}/capture',
                             json={'final_capture': True}, request_id=request_id).json()
        return {'capture_id': body['id'], 'status': body['status']}

    def void_authorization(self, authorization_id: str, *, request_id: str) -> None:
        self._request('POST', f'/v2/payments/authorizations/{authorization_id}/void', request_id=request_id)


class SimulatedPayPal:
    """Offline stand-in with the same state machine, so the app runs without sandbox credentials.

    The buyer approval page is replaced by a backend link that approves immediately.
    """
    mode = 'simulated'

    def __init__(self, backend_url: str):
        self._backend = backend_url.rstrip('/')
        self.orders: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}

    def create_order(self, *, reference_id, amount, currency, description, return_url, cancel_url) -> dict:
        order_id = 'SIM-' + uuid.uuid4().hex[:12].upper()
        self.orders[order_id] = {'id': order_id, 'status': 'PAYER_ACTION_REQUIRED', 'amount': f'{Decimal(amount):.2f}',
                                 'currency': currency, 'reference_id': reference_id}
        return {'id': order_id, 'status': 'PAYER_ACTION_REQUIRED',
                'approve_url': f'{self._backend}/paypal/simulate-approve?token={order_id}'}

    def get_order(self, order_id: str) -> dict:
        if order_id not in self.orders:
            raise PayPalError('order not found', 404)
        return self.orders[order_id]

    def authorize_order(self, order_id: str, *, request_id: str) -> dict:
        order = self.get_order(order_id)
        if order['status'] == 'COMPLETED':
            raise PayPalError('PayPal UNPROCESSABLE_ENTITY: ORDER_ALREADY_AUTHORIZED', 422)
        order['status'] = 'COMPLETED'
        auth_id = 'SIMAUTH-' + uuid.uuid4().hex[:10].upper()
        self.authorizations[auth_id] = {'status': 'CREATED', 'order_id': order_id}
        return {'order_status': 'COMPLETED', 'authorization_id': auth_id, 'status': 'CREATED', 'expiration_time': None}

    def _auth(self, authorization_id: str) -> dict:
        auth = self.authorizations.get(authorization_id)
        if auth is None:
            raise PayPalError('authorization not found', 404)
        if auth['status'] != 'CREATED':
            raise PayPalError(f"PayPal UNPROCESSABLE_ENTITY: AUTHORIZATION_ALREADY_{auth['status']}", 422)
        return auth

    def capture_authorization(self, authorization_id: str, *, request_id: str) -> dict:
        self._auth(authorization_id)['status'] = 'CAPTURED'
        return {'capture_id': 'SIMCAP-' + uuid.uuid4().hex[:10].upper(), 'status': 'COMPLETED'}

    def void_authorization(self, authorization_id: str, *, request_id: str) -> None:
        self._auth(authorization_id)['status'] = 'VOIDED'
