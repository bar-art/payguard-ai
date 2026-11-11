import json
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from app.ai import LLMClient, MandateReviewer, ShoppingAgent
from app.config import Settings, get_settings
from app.database import Database
from app.paypal_client import PaymentGateway, PayPalSandbox, SimulatedPayPal
from app.schemas import Mandate, PurchaseIntent, ReviewRequest, ShopRequest
from app.service import Conflict, GuardService, NotFound


def build_gateway(settings: Settings) -> PaymentGateway:
    if settings.paypal_configured:
        return PayPalSandbox(settings.paypal_client_id.get_secret_value(),
                             settings.paypal_client_secret.get_secret_value(), settings.paypal_base_url)
    return SimulatedPayPal(str(settings.backend_url))


def build_llm(settings: Settings) -> LLMClient | None:
    if not settings.llm_configured:
        return None
    return LLMClient(str(settings.llm_base_url), settings.llm_api_key.get_secret_value(),
                     settings.llm_model, settings.llm_timeout_s)


def create_app(settings: Settings | None = None, gateway: PaymentGateway | None = None,
               llm: LLMClient | None = None, use_llm: bool = True) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.database_url)
    gateway = gateway or build_gateway(settings)
    llm = llm if llm is not None else (build_llm(settings) if use_llm else None)
    service = GuardService(database, gateway, MandateReviewer(llm) if llm else None,
                           str(settings.backend_url), str(settings.dashboard_url), settings.paypal_return_to)
    catalog = json.loads(Path(settings.catalog_path).read_text())
    agent = ShoppingAgent(llm, catalog['products'])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        app.state.database = database
        app.state.service = service
        yield

    app = FastAPI(title='PayGuard AI', version='0.2.0', lifespan=lifespan,
                  description='A payment firewall for AI shopping agents, on PayPal Orders v2 (sandbox).')

    def not_found(exc: Exception):
        return HTTPException(404, str(exc))

    @app.get('/health')
    def health() -> dict[str, str]:
        with database.connect() as conn:
            conn.execute('SELECT 1').fetchone()
        return {'status': 'ok'}

    @app.get('/config')
    def config() -> dict:
        return {'paypal_mode': gateway.mode, 'ai_model': llm.model if llm else None,
                'ai_enabled': llm is not None, 'dashboard_url': str(settings.dashboard_url)}

    @app.get('/catalog')
    def get_catalog() -> dict:
        return catalog

    @app.post('/mandates', status_code=201)
    def create_mandate(mandate: Mandate) -> dict:
        return service.create_mandate(mandate).model_dump(mode='json')

    @app.get('/mandates')
    def list_mandates() -> list[dict]:
        return service.list_mandates()

    @app.post('/purchases', status_code=201)
    def submit_purchase(intent: PurchaseIntent) -> dict:
        """The endpoint an agent calls when it wants to pay."""
        try:
            return service.submit(intent)
        except NotFound as exc:
            raise not_found(exc)

    @app.get('/purchases')
    def list_purchases(status: str | None = None) -> list[dict]:
        return service.list_purchases(status)

    @app.get('/purchases/{purchase_id}')
    def get_purchase(purchase_id: str) -> dict:
        try:
            return service.get_purchase(purchase_id)
        except NotFound as exc:
            raise not_found(exc)

    @app.post('/purchases/{purchase_id}/review')
    def review(purchase_id: str, req: ReviewRequest) -> dict:
        try:
            return service.review(purchase_id, req)
        except NotFound as exc:
            raise not_found(exc)
        except Conflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/agent/shop', status_code=201)
    def agent_shop(req: ShopRequest) -> dict:
        """Let the demo shopping agent pick from the catalog, then submit through the guard."""
        try:
            mandate = service.get_mandate(req.mandate_id)
        except NotFound as exc:
            raise not_found(exc)
        products = catalog['products']
        if req.extra_product:
            custom = {**req.extra_product.model_dump(mode='json'), 'id': 'custom', 'currency': mandate.currency}
            products = products + [custom]
            if req.force:
                intent = agent.intent_for(req.mandate_id, custom, 'Chosen by the red-team playground (forced).')
                return service.submit(intent, origin='playground:forced')
        elif req.force:
            raise HTTPException(422, 'force needs extra_product')
        intent, chosen_by = ShoppingAgent(llm, products).propose(mandate, req.mandate_id, req.errand)
        origin = f'agent:{chosen_by}' + (':playground' if req.extra_product else '')
        return service.submit(intent, origin=origin)

    @app.get('/audit/verify')
    def verify_audit() -> dict:
        """Recompute the SHA-256 chain over every audit event."""
        return service.verify_audit()

    @app.post('/agent/scenario/{scenario_id}', status_code=201)
    def agent_scenario(scenario_id: str, mandate_id: str = Query(...)) -> dict:
        """Replay a scripted agent purchase (for example a hijacked agent) through the guard."""
        sc = next((s for s in catalog['scenarios'] if s['id'] == scenario_id), None)
        if sc is None:
            raise HTTPException(404, 'unknown scenario')
        product = next(p for p in catalog['products'] if p['id'] == sc['product_id'])
        try:
            intent = agent.intent_for(mandate_id, product, sc['rationale'])
            return service.submit(intent, origin=f'scenario:{scenario_id}')
        except NotFound as exc:
            raise not_found(exc)

    def back_to_dashboard(purchase: dict) -> RedirectResponse:
        return RedirectResponse(f"{settings.dashboard_url}?purchase={purchase['purchase_id']}&from=paypal",
                                status_code=303)

    @app.get('/paypal/return')
    def paypal_return(token: str) -> RedirectResponse:
        """PayPal sends the buyer here after approval; `token` is the order id."""
        try:
            return back_to_dashboard(service.buyer_approved(token))
        except NotFound as exc:
            raise not_found(exc)

    @app.get('/paypal/cancel')
    def paypal_cancel(token: str) -> RedirectResponse:
        try:
            return back_to_dashboard(service.buyer_cancelled(token))
        except NotFound as exc:
            raise not_found(exc)

    @app.get('/paypal/simulate-approve')
    def simulate_approve(token: str):
        """Only in simulated mode: stands in for the PayPal approval page."""
        if gateway.mode != 'simulated':
            raise HTTPException(404, 'only available without PayPal sandbox credentials')
        return paypal_return(token)

    return app


app = create_app()
