import io
import sqlite3
from datetime import date, datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app import core
from app.bot import Bot, TelegramError
from app.core import Ledger, ZONE, parse_add
from app.features import currency_fields
from app.receipts import extract_receipt, parse_receipt
from app.transfers import confirm_csv, export_csv, preview_csv
from app.web import create_app


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'today',lambda: date(2026,11,21))
    return Ledger(tmp_path/'ledger.sqlite3')


def test_migrates_v1_database_without_changing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'today',lambda: date(2026,11,21))
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE expenses(id INTEGER PRIMARY KEY,amount INTEGER NOT NULL,category TEXT NOT NULL,note TEXT NOT NULL,spent_on TEXT NOT NULL,source TEXT NOT NULL,source_key TEXT UNIQUE,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO expenses(amount,category,note,spent_on,source) VALUES (1250,'food','Old entry','2026-10-10','dashboard');""")
    ledger=Ledger(path)
    assert ledger.summary('2026-10')['total']==1250
    assert ledger.summary('2026-10')['expenses'][0]['original_currency'] is None
    assert Ledger(path).summary('2026-10')['total']==1250


def test_category_budget_carry_clear_and_history(ledger,monkeypatch):
    ledger.set_category_budget('2026-10','food','300')
    ledger.set_category_budget('2026-11','food','400')
    assert ledger.summary()['category_budgets']['food']==40000
    assert ledger.summary('2026-10')['category_budgets']['food']==30000
    monkeypatch.setattr(core,'today',lambda: date(2026,12,1))
    assert ledger.summary('2026-12')['category_budgets']['food']==40000
    ledger.set_category_budget('2026-12','food',None)
    assert ledger.summary('2026-12')['category_budgets']['food'] is None
    assert ledger.summary('2026-11')['category_budgets']['food']==40000


def test_alert_thresholds_and_durable_delivery(ledger):
    ledger.set_budget('2026-11','200')
    ledger.set_category_budget('2026-11','food','20')
    ledger.add_expense({'amount':'22','category':'food'})
    now=datetime(2026,11,21,12,tzinfo=ZONE)
    notifications=ledger.pending_notifications(now)
    total=next(n for n in notifications if 'Monthly budget alert' in n['text'])
    food=next(n for n in notifications if 'Food & drinks alert' in n['text'])
    assert '50%' in total['text']
    assert '100%' in food['text']
    assert len(food['keys'])==3
    for notification in notifications:
        ledger.mark_notification(notification['keys'],now)
    assert Ledger(ledger.path).pending_notifications(now)==[]
    ledger.add_expense({'amount':'30','category':'food'})
    assert '80%' in ledger.pending_notifications(now)[0]['text']
    ledger.set_settings({'budget_alerts':False})
    assert ledger.pending_notifications(now)==[]


def test_notification_failures_retry_not_marked(ledger,monkeypatch):
    ledger.set_budget('2026-11','100')
    bot=Bot(ledger,'test-token',123)
    monkeypatch.setattr(bot,'send',lambda text: (_ for _ in ()).throw(TelegramError('unavailable')))
    now=datetime(2026,11,21,12,tzinfo=ZONE)
    with pytest.raises(TelegramError):bot.tick(now)
    messages=[];monkeypatch.setattr(bot,'send',messages.append)
    bot.tick(now);bot.tick(now)
    assert len(messages)==1
    assert '100%' in messages[0]


def test_renewal_reminders_short_month_pause_and_restart(ledger,monkeypatch):
    ledger.save_subscription(None,{'name':'Test renewal','amount':'20','billing_day':31})
    monkeypatch.setattr(core,'today',lambda: date(2027,2,26))
    before=datetime(2027,2,26,8,tzinfo=ZONE)
    assert ledger.pending_notifications(before)==[]
    now=before.replace(hour=9)
    messages=ledger.pending_notifications(now)
    reminder=next(n for n in messages if 'Test renewal' in n['text'])
    assert '2027-02-28' in reminder['text']
    ledger.mark_notification(reminder['keys'],now)
    assert not any('Test renewal' in n['text'] for n in Ledger(ledger.path).pending_notifications(now))
    ledger.set_settings({'renewal_reminders':False})
    assert ledger.pending_notifications(now)==[]


def test_settings_validation(ledger):
    for payload in [{'thresholds':[]},{'thresholds':[0]},{'thresholds':[101]},{'thresholds':[True]},{'reminder_days':1.1},{'renewal_reminders':'yes'}]:
        with pytest.raises(ValueError):ledger.set_settings(payload)
    assert ledger.set_settings({'thresholds':[100,50,50]})['thresholds']==[50,100]


def test_goals_and_atomic_idempotent_contributions(ledger):
    identifier=ledger.save_goal(None,{'name':'Holiday','target':'2000','saved':'0.0','target_date':'2027-03-01'})
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: ledger.contribute_goal(identifier,'25','request-key-123'),range(3)))
    goal=ledger.summary()['goals'][0]
    assert goal['saved']==2500 and goal['target']==200000
    assert ledger.summary()['total']==12410
    with pytest.raises(ValueError):ledger.contribute_goal(identifier,'30','request-key-123')
    ledger.delete_goal(identifier)
    assert ledger.summary()['goals']==[]


def test_comparison_matches_elapsed_days(ledger):
    ledger.add_expense({'amount':'100','category':'food','spent_on':'2026-10-10'})
    ledger.add_expense({'amount':'90','category':'food','spent_on':'2026-10-30'})
    ledger.add_expense({'amount':'50','category':'food','spent_on':'2026-11-10'})
    compare=ledger.summary()['comparison']
    assert compare['has_history'] and compare['partial']
    assert compare['previous_total']==10000
    assert compare['previous_full_total']==19000
    assert compare['current_total']==17410
    assert compare['previous_through']=='2026-10-21'
    assert not ledger.summary('2026-09')['comparison']['has_history']


def test_original_currency_precision_and_totals(ledger):
    assert currency_fields({'original_currency':'jpy','original_amount':'1200'})==('JPY','1200')
    assert currency_fields({'original_currency':'KWD','original_amount':'1.234'})==('KWD','1.234')
    for payload in [{'original_currency':'JPY','original_amount':'1.1'},{'original_currency':'USD','original_amount':'1.001'},{'original_currency':'USD','original_amount':'1e999999'}, {'original_currency':'ZZZ','original_amount':'1'},{'original_amount':'3'}]:
        with pytest.raises(ValueError):currency_fields(payload)
    row=ledger.add_expense(parse_add('25 travel Lunch @2026-11-20 --original USD 18.50'))
    assert row['original_currency']=='USD' and row['original_amount']=='18.50'
    assert ledger.summary()['total']==14910
    ledger.update_expense(row['id'],{'amount':'26','category':'travel','note':'Corrected','original_currency':'USD','original_amount':'18.50'})
    assert ledger.summary()['total']==15010


def test_csv_review_confirm_atomic_dedup_and_roundtrip(ledger):
    for note in ['Lunch','=SUM(A1:A2)',"'=literal",'New\nline, comma']:
        ledger.add_expense({'amount':'3.50','category':'food','note':note})
    content=export_csv(ledger).encode()
    assert "'=SUM" in content.decode('utf-8-sig')
    preview=preview_csv(ledger,content)
    assert preview['new_rows']==0
    assert confirm_csv(ledger,preview['id'])['imported']==0
    content=b'spent_on,amount,category,note,original_currency,original_amount\n2026-10-10,25,travel,Foreign lunch,USD,18.50\n2026-10-10,25,travel,Foreign lunch,USD,18.50\n'
    preview=preview_csv(ledger,content)
    assert preview['new_rows']==2
    assert ledger.summary('2026-10')['total']==0
    assert confirm_csv(ledger,preview['id'])['imported']==2
    assert confirm_csv(ledger,preview['id'])['imported']==0
    assert confirm_csv(ledger,preview_csv(ledger,content)['id'])['imported']==0
    assert ledger.summary('2026-10')['total']==5000


def test_csv_invalid_row_saves_nothing_and_cancel(ledger):
    for content in [b'date,amount,category\n2026-10-01,1,food\n',b'spent_on,amount,category\n2026-10-10,2,food\n2026-10-11,1.001,food\n', b'\xff\xfe',b'spent_on,amount,category\n2026-10-10,2,food,extra\n']:
        with pytest.raises(ValueError):preview_csv(ledger,content)
    assert ledger.summary('2026-10')['total']==0
    preview=preview_csv(ledger,b'spent_on,amount,category\n2026-10-10,2,food\n')
    ledger.cancel_draft(preview['id'])
    with pytest.raises(ValueError):confirm_csv(ledger,preview['id'])


def test_receipt_dates_follow_the_ledger_clock(ledger, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2020, 1, 2))
    payload = parse_receipt('CAFE\n03/01/2020\nTOTAL SGD 5.00')
    assert payload['spent_on'] == '2020-01-02'
    ledger.validate_expense(payload)
    monkeypatch.setattr(core, 'today', lambda: date(2020, 1, 3))
    assert parse_receipt('CAFE\nTOTAL SGD 5.00')['spent_on'] == '2020-01-03'
    assert parse_receipt('CAFE\n03/01/2020\nTOTAL SGD 5.00')['spent_on'] == '2020-01-03'


def test_receipt_parse_and_explicit_confirmation(ledger):
    payload=parse_receipt('Noodle Cafe\n21/11/2026\nSubtotal 10.00\nGST 0.90\nTOTAL SGD 10.90\nCash 20.00\nChange 9.10')
    assert payload['amount']=='10.90' and payload['category']=='food' and payload['note']=='Noodle Cafe'
    assert payload['spent_on']=='2026-11-21'
    identifier=ledger.new_draft('receipt',payload)
    assert ledger.summary()['total']==12410
    row=ledger.confirm_receipt(identifier,payload)
    assert ledger.confirm_receipt(identifier,payload)['id']==row['id']
    assert ledger.summary()['total']==13500
    with pytest.raises(ValueError):ledger.cancel_draft(identifier)
    foreign=parse_receipt('Hotel ABC\nTOTAL USD 18.50')
    assert foreign['amount']=='' and foreign['original_currency']=='USD'
    identifier=ledger.new_draft('receipt',foreign)
    with pytest.raises(ValueError):ledger.confirm_receipt(identifier,foreign)
    foreign['amount']='25'
    ledger.confirm_receipt(identifier,foreign)
    identifier=ledger.new_draft('receipt',payload)
    ledger.cancel_draft(identifier)
    with pytest.raises(ValueError):ledger.confirm_receipt(identifier,payload)


def receipt_image():
    image=Image.new('RGB',(1000,650),'white')
    draw=ImageDraw.Draw(image)
    font=None
    for path in ['/System/Library/Fonts/Supplemental/Arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf','/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf']:
        if Path(path).exists():font=ImageFont.truetype(path,40);break
    if font is None:font=ImageFont.load_default(size=40)
    for i,line in enumerate(['NOODLE CAFE','21/11/2026','Subtotal 10.00','GST 0.90','TOTAL SGD 10.90','Thank you']):
        draw.text((60,40+i*80),line,fill='black',font=font)
    stream=io.BytesIO();image.save(stream,format='PNG');return stream.getvalue()


def test_real_local_ocr():
    result=extract_receipt(receipt_image())
    assert result['amount']=='10.90'
    assert 'NOODLE' in result['note'].upper()
    with pytest.raises(ValueError):extract_receipt(b'not an image')


def test_bot_receipt_callback_allowlist_and_dedup(ledger,monkeypatch):
    payload=parse_receipt('CAFE\nTOTAL SGD 5.00')
    identifier=ledger.new_draft('receipt',payload)
    bot=Bot(ledger,'test-token',123)
    sent=[];monkeypatch.setattr(bot,'send',sent.append)
    monkeypatch.setattr(bot,'telegram',lambda method,**payload: (sent.append(payload['text']) if method=='sendMessage' else None) or {'message_id':1})
    callback={'id':'callback1','from':{'id':999},'message':{'chat':{'id':123,'type':'private'}},'data':'receipt-save:'+identifier}
    bot.process_update({'callback_query':callback})
    assert ledger.summary()['total']==12410
    callback['from']['id']=123
    bot.process_update({'callback_query':callback});bot.process_update({'callback_query':callback})
    assert ledger.summary()['total']==12910
    assert len(sent)==2


def test_feature_api_auth_and_uploads(ledger,monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD','testing-password-123')
    headers={'X-Penny-Client':'dashboard'}
    with TestClient(create_app(ledger)) as client:
        assert client.get('/api/csv/export').status_code==401
        assert client.post('/api/goals',headers=headers,json={'name':'x','target':1}).status_code==401
        client.post('/api/login',headers=headers,json={'password':'testing-password-123'})
        assert client.put('/api/category-budgets/2026-11/food',headers=headers,json={'amount':'300'}).status_code==200
        goal=client.post('/api/goals',headers=headers,json={'name':'Holiday','target':'2000'}).json()['id']
        assert client.post(f'/api/goals/{goal}/contributions',headers=headers,json={'amount':25,'key':'test-request-123'}).status_code==200
        assert client.put('/api/notification-settings',headers=headers,json={'reminder_days':2}).status_code==200
        result=client.post('/api/csv/preview',headers=headers,files={'file':('test.csv',b'spent_on,amount,category\n2026-10-10,5,food\n','text/csv')})
        assert result.status_code==200
        assert client.post('/api/csv/'+result.json()['id']+'/confirm',headers=headers).json()['imported']==1
        export=client.get('/api/csv/export?month=2026-10')
        assert export.status_code==200 and 'attachment' in export.headers['content-disposition']
        receipt=client.post('/api/receipts/preview',headers=headers,files={'file':('receipt.png',receipt_image(),'image/png')})
        assert receipt.status_code==200
        payload=receipt.json()['payload']
        assert client.post('/api/receipts/'+receipt.json()['id']+'/confirm',headers=headers,json=payload).status_code==200
