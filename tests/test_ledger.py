from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import core
from app.bot import Bot, TelegramError, due_recaps, summary_text
from app.core import Ledger, ZONE, cents, parse_add
from app.graphs import render_summary
from app.web import create_app


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 21))
    return Ledger(tmp_path / 'ledger.sqlite3')


@pytest.mark.parametrize('value,expected', [('0.10',10),('10.10',1010),('98',9800),('16.00',1600),('1e2',10000)])
def test_exact_money(value, expected):
    assert cents(value) == expected


@pytest.mark.parametrize('value', ['0','-1','1.001','NaN','Infinity','10000000',None,'nope'])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        cents(value)


def test_seed_and_recurring_are_idempotent(ledger):
    assert ledger.summary('2026-11')['total'] == 12410
    assert ledger.summary('2026-11')['count'] == 3
    assert Ledger(ledger.path).summary('2026-11')['count'] == 3
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ledger.sync_subscriptions(), range(12)))
    assert ledger.summary('2026-11')['count'] == 3
    assert ledger.summary('2026-10')['count'] == 0


def test_cents_sum_without_float_drift(ledger):
    for _ in range(10):
        ledger.add_expense({'amount':'.10','category':'food'})
    result = ledger.summary('2026-11')
    assert result['total'] == 12510
    assert next(cat['amount'] for cat in result['categories'] if cat['id']=='food') == 100


def test_parser_and_date(ledger):
    payload = parse_add('S$12.50 personal-care Haircut @2026-11-20')
    assert payload == {'amount':'12.50','category':'personal_care','note':'Haircut','spent_on':'2026-11-20'}
    assert parse_add('2 transport', date(2026,10,31))['spent_on'] == '2026-10-31'
    for text in ['12', '12 unknown x', '1.001 food x', '1 food x @bad']:
        with pytest.raises(ValueError):
            parse_add(text)
    with pytest.raises(ValueError):
        ledger.add_expense({'amount':'1','category':'food','spent_on':'2026-11-22'})


def test_bot_dedup_and_deleted_expense_stays_deleted(ledger):
    payload = {'amount':'5','category':'food'}
    a = ledger.add_expense(payload, 'telegram', 'telegram:1:1')
    b = ledger.add_expense(payload, 'telegram', 'telegram:1:1')
    assert a['id'] == b['id']
    ledger.delete_expense(a['id'])
    ledger.add_expense(payload, 'telegram', 'telegram:1:1')
    assert ledger.summary()['total'] == 12410
    subscription = ledger.summary()['expenses'][0]
    ledger.delete_expense(subscription['id'])
    ledger.sync_subscriptions()
    assert ledger.summary()['count'] == 2


def test_budget_carries_forward_and_keeps_past_stable(ledger):
    assert ledger.summary()['budget'] is None
    ledger.set_budget('2026-10','1000')
    ledger.set_budget('2026-11','1500')
    assert ledger.summary('2026-10')['budget'] == 100000
    assert ledger.summary('2026-11')['remaining'] == 150000-12410
    ledger.add_expense({'amount':'2000','category':'travel'})
    assert ledger.summary()['remaining'] < 0
    with pytest.raises(ValueError):
        ledger.set_budget('2026-12','100')


def test_short_month_and_recurring_catchup(ledger, monkeypatch):
    ledger.save_subscription(None, {'name':'Test','amount':'20','billing_day':31})
    monkeypatch.setattr(core, 'today', lambda: date(2027, 2, 28))
    ledger.sync_subscriptions()
    result=ledger.summary('2027-02')
    assert result['total'] == 14410
    assert next(e for e in result['expenses'] if e['note']=='Test')['spent_on'] == '2027-02-28'
    assert ledger.summary('2026-12')['total'] == 14410
    ledger.sync_subscriptions()
    assert ledger.summary('2027-02')['count'] == 4


def test_subscription_edits_preserve_posted_entries(ledger, monkeypatch):
    old = ledger.summary()['subscriptions'][0]
    ledger.save_subscription(old['id'], {'name':'Gym new','amount':'100','billing_day':2,'active':True})
    assert ledger.summary()['total'] == 12410
    monkeypatch.setattr(core,'today',lambda: date(2026,12,3))
    assert ledger.summary('2026-12')['total'] == 12610
    ledger.save_subscription(old['id'], {'name':'Gym new','amount':'100','billing_day':2,'active':False})
    monkeypatch.setattr(core,'today',lambda: date(2027,1,3))
    assert ledger.summary('2027-01')['total'] == 2610


def test_invalid_month_and_subscription_fields(ledger):
    for month in ['2026-13','2026-1','bad','2026-12']:
        with pytest.raises(ValueError):
            ledger.summary(month)
    for payload in [{'name':'','amount':1}, {'name':'x','amount':1,'billing_day':1.5}, {'name':'x','amount':1,'active':'true'}]:
        with pytest.raises(ValueError):
            ledger.save_subscription(None,payload)


def test_recap_schedule_timezone_and_leap_year():
    assert due_recaps(datetime(2026,11,30,23,58,tzinfo=ZONE),'2026-11') == []
    assert due_recaps(datetime(2026,11,30,23,59,tzinfo=ZONE),'2026-11') == ['2026-11']
    assert due_recaps(datetime(2026,12,1,1,0,tzinfo=ZONE),'2026-11') == ['2026-11']
    assert due_recaps(datetime(2028,2,28,23,59,tzinfo=ZONE),'2028-02') == []
    assert due_recaps(datetime(2028,2,29,23,59,tzinfo=ZONE),'2028-02') == ['2028-02']
    assert due_recaps(datetime(2027,2,1,tzinfo=ZONE),'2026-11') == ['2026-11','2026-12','2027-01']


def test_bot_allowlist_duplicate_updates_and_recap_delivery(ledger, monkeypatch):
    bot = Bot(ledger, 'test-token', 123)
    sent=[]
    monkeypatch.setattr(bot,'send',sent.append)
    calls=[]
    monkeypatch.setattr(bot,'telegram',lambda method,**payload: calls.append((method,payload)) or {'message_id':len(calls)})
    update={'update_id':10,'message':{'text':'/add 2 food Lunch','from':{'id':999},'chat':{'id':123,'type':'private'}}}
    bot.process_update(update)
    assert not sent
    update['message']['from']['id']=123
    update['message']['chat']['type']='group'
    bot.process_update(update)
    assert not sent
    update['message']['chat']['type']='private'
    bot.process_update(update)
    bot.process_update(update)
    assert ledger.summary()['total'] == 12410
    data=calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    callback={'callback_query':{'id':'confirm','from':{'id':123},'message':{'chat':{'id':123,'type':'private'}},'data':data}}
    bot.process_update(callback);bot.process_update(callback)
    assert ledger.summary()['total'] == 12610
    bot.process_update({'update_id':11,'message':{'text':'/add 1.001 food no','from':{'id':123},'chat':{'id':123,'type':'private'}}})
    assert 'two decimal places' in sent[-1]
    recaps=[]
    monkeypatch.setattr(bot,'send_recap',recaps.append)
    now=datetime(2026,11,30,23,59,tzinfo=ZONE)
    bot.tick(now)
    bot.tick(now)
    assert recaps == ['2026-11']


def test_failed_recap_is_retried(ledger, monkeypatch):
    bot=Bot(ledger,'test-token',123)
    monkeypatch.setattr(bot,'send',lambda text: None)
    def fail(month):
        raise TelegramError('unavailable')
    monkeypatch.setattr(bot,'send_recap',fail)
    with pytest.raises(TelegramError):
        bot.tick(datetime(2026,11,30,23,59,tzinfo=ZONE))
    recaps=[]
    monkeypatch.setattr(bot,'send_recap',recaps.append)
    monkeypatch.setattr(core,'today',lambda: date(2026,12,1))
    bot.tick(datetime(2026,12,1,1,tzinfo=ZONE))
    assert recaps == ['2026-11']


def test_graph_and_telegram_caption(ledger):
    for key in core.CATEGORIES:
        ledger.add_expense({'amount':'9999999.99','category':key})
    ledger.set_budget('2026-11','9999999.99')
    summary=ledger.summary()
    assert len(summary_text(summary)) <= 1024
    assert render_summary(summary).startswith(b'\x89PNG\r\n\x1a\n')
    assert render_summary(ledger.summary('2026-10')).startswith(b'\x89PNG')


def test_web_auth_csrf_and_crud(ledger, monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD','testing-password-123')
    with TestClient(create_app(ledger)) as client:
        assert client.get('/api/health').json() == {'status':'ok'}
        assert client.get('/api/summary').status_code == 401
        assert client.post('/api/login',json={'password':'testing-password-123'}).status_code == 403
        headers={'X-Penny-Client':'dashboard'}
        assert client.post('/api/login',headers=headers,json={'password':'wrong'}).status_code == 401
        login = client.post('/api/login',headers=headers,json={'password':'testing-password-123'})
        assert login.status_code == 200
        assert 'httponly' in login.headers['set-cookie'].lower()
        assert client.get('/api/summary').json()['total'] == 12410
        assert client.post('/api/expenses',headers={**headers,'Origin':'https://evil.test'},json={'amount':'1','category':'food'}).status_code == 403
        result=client.post('/api/expenses',headers=headers,json={'amount':'2.20','category':'transport','note':'MRT'})
        assert result.status_code == 201
        identifier=result.json()['id']
        assert client.put(f'/api/expenses/{identifier}',headers=headers,json={'amount':'3.20','category':'transport'}).status_code == 200
        assert client.put('/api/budgets/2026-11',headers=headers,json={'amount':'1000'}).status_code == 200
        assert client.get('/api/summary').json()['remaining'] == 100000-12730
        assert client.delete(f'/api/expenses/{identifier}',headers=headers).status_code == 200
        assert client.delete(f'/api/expenses/{identifier}',headers=headers).status_code == 404
        assert client.post('/api/logout',headers=headers).status_code == 200
        assert client.get('/api/summary').status_code == 401


def test_login_rate_limit_and_password_requirement(ledger, monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD','testing-password-123')
    with TestClient(create_app(ledger)) as client:
        for _ in range(10):
            assert client.post('/api/login',headers={'X-Penny-Client':'dashboard'},json={'password':'wrong'}).status_code == 401
        assert client.post('/api/login',headers={'X-Penny-Client':'dashboard'},json={'password':'wrong'}).status_code == 429
    monkeypatch.setenv('DASHBOARD_PASSWORD','short')
    with pytest.raises(RuntimeError):
        with TestClient(create_app(ledger)):
            pass
