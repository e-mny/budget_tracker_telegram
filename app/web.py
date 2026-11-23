import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import Ledger
from .receipts import MAX_RECEIPT, extract_receipt
from .transfers import MAX_CSV, confirm_csv, export_csv, preview_csv

STATIC = Path(__file__).parent / 'static'


def create_app(ledger=None):
    store = ledger or Ledger()
    password = os.getenv('DASHBOARD_PASSWORD', '')
    secure_cookie = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
    failures = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        if len(password) < 12:
            raise RuntimeError('Set DASHBOARD_PASSWORD to at least 12 characters in .env before starting.')
        store.sync_subscriptions()
        yield

    app = FastAPI(title='Penny', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ledger = store

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, status_code=422)

    @app.exception_handler(LookupError)
    async def missing(request, error):
        return JSONResponse({'detail': str(error)}, status_code=404)

    @app.middleware('http')
    async def headers(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            # A foreign page cannot set this header without a CORS preflight, which we deny.
            if request.headers.get('x-penny-client') != 'dashboard':
                return JSONResponse({'detail': 'Invalid request origin.'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.url.netloc}':
                return JSONResponse({'detail': 'Invalid request origin.'}, status_code=403)
        result = await call_next(request)
        result.headers['Cache-Control'] = 'no-store'
        result.headers['X-Content-Type-Options'] = 'nosniff'
        result.headers['X-Frame-Options'] = 'DENY'
        result.headers['Referrer-Policy'] = 'no-referrer'
        result.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return result

    @app.get('/api/health')
    def health():
        with store.db() as db:
            db.execute('SELECT 1')
        return {'status': 'ok'}

    @app.post('/api/login')
    def login(request: Request, payload: dict, response: Response):
        host = request.client.host if request.client else 'unknown'
        now = time.time()
        attempts = failures[host]
        while attempts and attempts[0] < now - 900:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, 'Too many login attempts. Try again in 15 minutes.')
        supplied = payload.get('password', '')
        if not isinstance(supplied, str) or len(password) < 12 or not hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(password.encode()).digest()
        ):
            attempts.append(now)
            raise HTTPException(401, 'Incorrect password.')
        attempts.clear()
        token = secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (int(now),))
            db.execute('INSERT INTO sessions VALUES (?,?)', (hmac.new(password.encode(), token.encode(), hashlib.sha256).hexdigest(), int(now) + 86400 * 30))
        response.set_cookie('penny_session', token, max_age=86400 * 30, httponly=True,
                            secure=secure_cookie, samesite='strict')
        return {'ok': True}

    def owner(request: Request):
        token = request.cookies.get('penny_session', '')
        with store.db() as db:
            row = db.execute('SELECT expires FROM sessions WHERE token_hash=?', (hmac.new(password.encode(), token.encode(), hashlib.sha256).hexdigest(),)).fetchone()
        if not row or row['expires'] < time.time():
            raise HTTPException(401, 'Sign in to view your expenses.')

    api = APIRouter(prefix='/api', dependencies=[Depends(owner)])

    @api.post('/logout')
    def logout(request: Request, response: Response):
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',
                       (hmac.new(password.encode(), request.cookies.get('penny_session', '').encode(), hashlib.sha256).hexdigest(),))
        response.delete_cookie('penny_session')
        return {'ok': True}

    @api.get('/summary')
    def summary(month: str | None = None):
        return store.summary(month)

    @api.post('/expenses', status_code=201)
    def add_expense(payload: dict):
        return store.add_expense(payload)

    @api.post('/income', status_code=201)
    def add_income(payload: dict):
        return {'id': store.save_income(None, payload)}

    @api.put('/income/{identifier}')
    def edit_income(identifier: int, payload: dict):
        store.save_income(identifier, payload)
        return {'ok': True}

    @api.delete('/income/{identifier}')
    def delete_income(identifier: int):
        store.delete_income(identifier)
        return {'ok': True}

    @api.put('/expenses/{identifier}')
    def update_expense(identifier: int, payload: dict):
        store.update_expense(identifier, payload)
        return {'ok': True}

    @api.delete('/expenses/{identifier}')
    def delete_expense(identifier: int):
        store.delete_expense(identifier)
        return {'ok': True}

    @api.put('/budgets/{month}')
    def budget(month: str, payload: dict):
        store.set_budget(month, payload.get('amount'))
        return {'ok': True}

    @api.post('/subscriptions', status_code=201)
    def add_subscription(payload: dict):
        store.save_subscription(None, payload)
        return {'ok': True}

    @api.put('/subscriptions/{identifier}')
    def update_subscription(identifier: int, payload: dict):
        store.save_subscription(identifier, payload)
        return {'ok': True}

    @api.put('/category-budgets/{month}/{cat}')
    def category_budget(month: str, cat: str, payload: dict):
        store.set_category_budget(month, cat, payload.get('amount'))
        return {'ok': True}

    @api.put('/notification-settings')
    def notification_settings(payload: dict):
        return store.set_settings(payload)

    @api.post('/goals', status_code=201)
    def add_goal(payload: dict):
        return {'id': store.save_goal(None, payload)}

    @api.put('/goals/{identifier}')
    def update_goal(identifier: int, payload: dict):
        return {'id': store.save_goal(identifier, payload)}

    @api.post('/goals/{identifier}/contributions')
    def contribute_goal(identifier: int, payload: dict):
        store.contribute_goal(identifier, payload.get('amount'), payload.get('key'))
        return {'ok': True}

    @api.delete('/goals/{identifier}')
    def delete_goal(identifier: int):
        store.delete_goal(identifier)
        return {'ok': True}

    @api.get('/csv/export')
    def csv_export(month: str | None = None):
        return Response(export_csv(store, month), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="penny-expenses.csv"'})

    @api.post('/csv/preview')
    async def csv_preview(file: UploadFile):
        content = await file.read(MAX_CSV + 1)
        await file.close()
        return await run_in_threadpool(preview_csv, store, content)

    @api.post('/csv/{identifier}/confirm')
    def csv_confirm(identifier: str):
        return confirm_csv(store, identifier)

    @api.post('/receipts/preview')
    async def receipt_preview(file: UploadFile):
        content = await file.read(MAX_RECEIPT + 1)
        await file.close()
        payload = await run_in_threadpool(extract_receipt, content)
        identifier = store.new_draft('receipt', payload)
        return {'id': identifier, 'payload': payload}

    @api.post('/receipts/{identifier}/confirm')
    def receipt_confirm(identifier: str, payload: dict):
        return store.confirm_receipt(identifier, payload)

    @api.post('/drafts/{identifier}/cancel')
    def cancel_draft(identifier: str):
        store.cancel_draft(identifier)
        return {'ok': True}

    app.include_router(api)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    return app
