import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import core
from app.core import Ledger, ZONE
from app.web import create_app


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 21))
    return Ledger(tmp_path / 'ledger.sqlite3')


def charges(ledger, name):
    with ledger.db() as db:
        return [dict(r) for r in db.execute('SELECT * FROM expenses WHERE note=? AND deleted=0 ORDER BY spent_on', (name,))]


@pytest.mark.parametrize('count,unit,anchor,as_of,expected', [
    (3, 'months', '2026-01-31', '2026-11-21', ['2026-01-31', '2026-04-30', '2026-07-31', '2026-10-31']),
    (6, 'months', '2026-02-28', '2026-11-21', ['2026-02-28', '2026-08-28']),
    (1, 'years', '2026-05-12', '2028-05-12', ['2026-05-12', '2027-05-12', '2028-05-12']),
    (1, 'years', '2024-02-29', '2028-02-29', ['2024-02-29', '2025-02-28', '2026-02-28', '2027-02-28', '2028-02-29']),
    (2, 'weeks', '2026-11-01', '2026-12-13', ['2026-11-01', '2026-11-15', '2026-11-29', '2026-12-13']),
    (10, 'days', '2026-11-01', '2026-12-01', ['2026-11-01', '2026-11-11', '2026-11-21', '2026-12-01']),
])
def test_custom_recurrence_catchup_and_restart(ledger, monkeypatch, count, unit, anchor, as_of, expected):
    ledger.save_subscription(None, {'name': 'Plan', 'amount': '120', 'interval_count': count,
                                   'interval_unit': unit, 'start_date': anchor})
    monkeypatch.setattr(core, 'today', lambda: date.fromisoformat(as_of))
    reopened = Ledger(ledger.path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: reopened.sync_subscriptions(), range(8)))
    rows = charges(ledger, 'Plan')
    assert [r['spent_on'] for r in rows] == expected
    assert all(r['amount'] == 12000 for r in rows)
    ledger.delete_expense(rows[-1]['id'])
    ledger.sync_subscriptions()
    assert len(charges(ledger, 'Plan')) == len(expected) - 1


def test_future_anchor_next_due_and_estimated_totals(ledger):
    ledger.save_subscription(None, {'name': 'Annual', 'amount': '120', 'interval_unit': 'years', 'start_date': '2028-05-12'})
    ledger.save_subscription(None, {'name': 'Quarterly', 'amount': '30', 'interval_count': 3, 'start_date': '2026-12-31'})
    ledger.save_subscription(None, {'name': 'Weekly', 'amount': '7', 'interval_unit': 'weeks', 'start_date': '2026-12-01'})
    summary = ledger.summary()
    assert charges(ledger, 'Annual') == charges(ledger, 'Quarterly') == charges(ledger, 'Weekly') == []
    assert next(s['next_due'] for s in summary['upcoming_subscriptions'] if s['name'] == 'Annual') == '2028-05-12'
    annual = (12410 + 40120) * 12 + 12000 + 3000 * 4 + 36500
    assert summary['annual_subscription_total'] == annual
    assert summary['subscription_total'] == round(annual / 12)


def test_schedule_edits_preserve_history_without_backfill(ledger, monkeypatch):
    old = ledger.summary()['subscriptions'][0]
    ledger.save_subscription(old['id'], {'name': 'Gym annual', 'amount': '1000', 'interval_unit': 'years',
                                         'start_date': '2026-12-15'})
    assert ledger.summary()['total'] == 12410
    monkeypatch.setattr(core, 'today', lambda: date(2027, 2, 21))
    ledger.sync_subscriptions()
    assert [r['spent_on'] for r in charges(ledger, 'Gym annual')] == ['2026-12-15']
    ledger.save_subscription(old['id'], {'name': 'Gym weekly', 'amount': '10', 'interval_unit': 'weeks',
                                         'start_date': '2026-11-01'})
    assert [r['spent_on'] for r in charges(ledger, 'Gym weekly')] == ['2027-02-21']
    monkeypatch.setattr(core, 'today', lambda: date(2027, 2, 28))
    ledger.sync_subscriptions()
    assert [r['spent_on'] for r in charges(ledger, 'Gym weekly')] == ['2027-02-21', '2027-02-28']


def test_pause_resume_does_not_charge_missed_periods(ledger, monkeypatch):
    ledger.save_subscription(None, {'name': 'Weekly', 'amount': '10', 'interval_unit': 'weeks', 'start_date': '2026-11-21'})
    sub = next(s for s in ledger.summary()['subscriptions'] if s['name'] == 'Weekly')
    ledger.save_subscription(sub['id'], {'name': 'Weekly', 'amount': '10', 'active': False})
    monkeypatch.setattr(core, 'today', lambda: date(2027, 1, 2))
    ledger.save_subscription(sub['id'], {'name': 'Weekly', 'amount': '10', 'active': True})
    assert [r['spent_on'] for r in charges(ledger, 'Weekly')] == ['2026-11-21', '2027-01-02']


def test_nonmonthly_reminder_uses_actual_renewal_and_deduplicates(ledger, monkeypatch):
    ledger.save_subscription(None, {'name': 'Annual', 'amount': '120', 'interval_unit': 'years', 'start_date': '2026-11-24'})
    now = datetime(2026, 11, 21, 9, tzinfo=ZONE)
    reminder = next(n for n in ledger.pending_notifications(now) if 'Annual' in n['text'])
    assert '2026-11-24' in reminder['text'] and 'S$120.00' in reminder['text']
    ledger.mark_notification(reminder['keys'], now)
    assert not any('Annual' in n['text'] for n in Ledger(ledger.path).pending_notifications(now))
    monkeypatch.setattr(core, 'today', lambda: date(2027, 1, 21))
    assert not any('Annual' in n['text'] for n in ledger.pending_notifications(now.replace(year=2027, month=1)))


def test_migrates_monthly_schedule_and_deleted_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2027, 2, 28))
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('start_month','2027-01');
            CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,amount INTEGER NOT NULL,
                billing_day INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,start_month TEXT NOT NULL);
            INSERT INTO subscriptions VALUES (1,'Old plan',1000,31,1,'2027-01');
            CREATE TABLE expenses(id INTEGER PRIMARY KEY,amount INTEGER NOT NULL,category TEXT NOT NULL,
                note TEXT NOT NULL,spent_on TEXT NOT NULL,source TEXT NOT NULL,source_key TEXT UNIQUE,
                deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO expenses VALUES (1,1000,'subscription','Old plan','2027-01-31',
                'subscription','subscription:1:2027-01',1,CURRENT_TIMESTAMP);
        ''')
    ledger = Ledger(path)
    assert ledger.summary()['subscriptions'][0]['interval_unit'] == 'months'
    assert [r['spent_on'] for r in charges(ledger, 'Old plan')] == ['2027-02-28']
    assert Ledger(path).summary()['total'] == 1000 + 40120
    # Saving a migrated schedule keeps the original day 31 after February.
    ledger.save_subscription(1, {'name': 'Old plan', 'amount': '10', 'start_date': '2027-01-31'})
    monkeypatch.setattr(core, 'today', lambda: date(2027, 3, 31))
    ledger.sync_subscriptions()
    assert charges(ledger, 'Old plan')[-1]['spent_on'] == '2027-03-31'


@pytest.mark.parametrize('fields', [
    {'interval_count': 0}, {'interval_count': 121}, {'interval_count': True}, {'interval_count': 1.5},
    {'interval_count': '3'}, {'interval_unit': 'fortnights'}, {'interval_unit': []},
    {'start_date': '2026-02-30'}, {'start_date': None}, {'start_date': ''},
])
def test_invalid_schedule_does_not_write(ledger, fields):
    with pytest.raises(ValueError):
        ledger.save_subscription(None, {'name': 'Invalid', 'amount': '10', **fields})
    assert not any(s['name'] == 'Invalid' for s in ledger.summary()['subscriptions'])


def test_subscription_api_roundtrip(ledger, monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD', 'testing-password-123')
    headers = {'X-Penny-Client': 'dashboard'}
    with TestClient(create_app(ledger)) as client:
        client.post('/api/login', headers=headers, json={'password': 'testing-password-123'})
        payload = {'name': 'Annual', 'amount': '120', 'interval_count': 1, 'interval_unit': 'years', 'start_date': '2027-03-12'}
        assert client.post('/api/subscriptions', headers=headers, json=payload).status_code == 201
        sub = next(s for s in client.get('/api/summary').json()['subscriptions'] if s['name'] == 'Annual')
        assert (sub['interval_count'], sub['interval_unit'], sub['start_date']) == (1, 'years', '2027-03-12')
        assert client.put(f"/api/subscriptions/{sub['id']}", headers=headers, json={**payload, 'interval_count': 2}).status_code == 200
        assert client.post('/api/subscriptions', headers=headers, json={**payload, 'interval_count': 0}).status_code == 422


def test_recurring_household_contribution_uses_selected_category(ledger, monkeypatch):
    ledger.save_subscription(None, {'name': 'Household allowance', 'amount': '401.20',
                                   'category': 'home', 'start_date': '2026-11-21'})
    summary = ledger.summary()
    sub = next(s for s in summary['subscriptions'] if s['name'] == 'Household allowance')
    assert sub['category'] == 'home'
    assert next(c['amount'] for c in summary['categories'] if c['id'] == 'home') == 40120 + 1010
    assert summary['total'] == 12410 + 40120
    # Existing clients can omit category when updating the payment.
    ledger.save_subscription(sub['id'], {'name': 'Household allowance', 'amount': '401.20'})
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 21))
    reopened = Ledger(ledger.path)
    reopened.sync_subscriptions()
    rows = charges(reopened, 'Household allowance')
    assert [(r['spent_on'], r['amount'], r['category']) for r in rows] == [
        ('2026-11-21', 40120, 'home'), ('2026-12-21', 40120, 'home')]


def test_recurring_category_edit_preserves_recorded_history(ledger, monkeypatch):
    sub = ledger.summary()['subscriptions'][0]
    ledger.save_subscription(sub['id'], {'name': sub['name'], 'amount': '98', 'category': 'home'})
    assert charges(ledger, sub['name'])[0]['category'] == 'health'
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 1))
    ledger.sync_subscriptions()
    assert charges(ledger, sub['name'])[-1]['category'] == 'home'


def test_invalid_recurring_category_rejects_update(ledger):
    sub = ledger.summary()['subscriptions'][0]
    with pytest.raises(ValueError):
        ledger.save_subscription(sub['id'], {'name': 'Bad category', 'amount': '98', 'category': 'unknown'})
    assert ledger.summary()['subscriptions'][0] == sub
