from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import core
from app.core import Ledger
from app.web import create_app


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 23))
    return Ledger(tmp_path / 'ledger.sqlite3')


def test_income_sources_net_flow_and_month_isolation(ledger):
    salary = ledger.save_income(None, {'source': 'Salary', 'amount': '3000', 'received_on': '2026-11-04'})
    ledger.save_income(None, {'source': 'Freelance', 'amount': '100.25'})
    ledger.save_income(None, {'source': 'Salary', 'amount': '200', 'received_on': '2026-10-04'})
    result = ledger.summary()
    assert result['income_total'] == 310025
    assert result['net_cashflow'] == 310025 - 12410 - 60000
    assert result['income_sources'] == [{'name': 'Freelance', 'amount': 10025}, {'name': 'Salary', 'amount': 300000}]
    assert ledger.summary('2026-10')['income_total'] == 20000
    assert result['total'] == 12410 and result['budget'] is None
    ledger.save_income(salary, {'source': 'Salary', 'amount': '3200', 'received_on': '2026-11-04'})
    assert ledger.summary()['income_total'] == 330025
    ledger.delete_income(salary)
    assert ledger.summary()['income_total'] == 10025
    with pytest.raises(LookupError):
        ledger.delete_income(salary)


def test_priorities_are_personal_and_do_not_change_total(ledger):
    result = ledger.summary()
    assert result['spending_priorities'] == {'necessity': 10810, 'non_necessity': 0, 'unclassified': 1600}
    row = ledger.add_expense({'amount': '20', 'category': 'health', 'note': 'Insurance'})
    other = ledger.add_expense({'amount': '10', 'category': 'health', 'note': 'Optional health purchase', 'necessity': 'non_necessity'})
    result = ledger.summary()
    assert result['spending_priorities'] == {'necessity': 12810, 'non_necessity': 1000, 'unclassified': 1600}
    assert sum(result['spending_priorities'].values()) == result['total']
    ledger.update_expense(other['id'], {'amount': '10', 'category': 'health', 'note': 'Changed description'})
    assert ledger.summary()['spending_priorities']['non_necessity'] == 1000
    ledger.update_expense(row['id'], {'amount': '20', 'category': 'health', 'note': 'Insurance', 'necessity': 'non_necessity'})
    assert ledger.summary()['spending_priorities']['non_necessity'] == 3000
    ledger.update_expense(row['id'], {'amount': '20', 'category': 'health', 'note': 'Insurance', 'necessity': None})
    assert ledger.summary()['spending_priorities']['necessity'] == 12810
    ledger.delete_expense(other['id'])
    assert ledger.summary()['spending_priorities']['non_necessity'] == 0


def test_recurring_income_due_catchup_deleted_key_and_pause(ledger, monkeypatch):
    ledger.save_subscription(None, {'name': 'Salary', 'amount': '3000', 'payment_type': 'income', 'start_date': '2026-11-25'})
    assert ledger.summary()['income_total'] == 0
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 25))
    reopened = Ledger(ledger.path)
    result = reopened.summary()
    assert result['income_total'] == 300000
    assert reopened.summary('2026-11')['income_total'] == 300000
    reopened.sync_subscriptions()
    assert len(reopened.summary()['income_entries']) == 1
    row = result['income_entries'][0]
    reopened.delete_income(row['id'])
    assert reopened.summary()['income_total'] == 0
    sub = next(s for s in result['subscriptions'] if s['name'] == 'Salary')
    reopened.save_subscription(sub['id'], {'name': 'Salary', 'amount': '3000', 'active': False})
    monkeypatch.setattr(core, 'today', lambda: date(2027, 1, 25))
    assert reopened.summary()['income_total'] == 0


def test_recurring_priority_only_changes_future_entries(ledger, monkeypatch):
    sub = ledger.summary()['subscriptions'][0]
    ledger.save_subscription(sub['id'], {'name': sub['name'], 'amount': '98', 'necessity': 'non_necessity'})
    assert ledger.summary()['spending_priorities']['necessity'] == 10810
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 1))
    assert ledger.summary()['spending_priorities']['non_necessity'] == 9800
    assert ledger.summary('2026-11')['spending_priorities']['necessity'] == 10810 + 40120


@pytest.mark.parametrize('payload', [
    {'source': '', 'amount': '1'}, {'source': 'Salary', 'amount': '1.001'},
    {'source': 'Salary', 'amount': '1', 'received_on': '2026-11-24'},
    {'source': 'Salary', 'amount': '1', 'received_on': 'bad'},
    {'source': 'Salary', 'amount': '1', 'note': 'x' * 241},
])
def test_invalid_income_is_not_recorded(ledger, payload):
    with pytest.raises(ValueError):
        ledger.save_income(None, payload)
    assert ledger.summary()['income_entries'] == []


def test_invalid_priority_and_receipt_priority(ledger):
    with pytest.raises(ValueError):
        ledger.add_expense({'amount': '5', 'category': 'food', 'necessity': 'maybe'})
    payload = {'amount': '5', 'category': 'food', 'note': 'Lunch', 'necessity': 'necessity'}
    draft = ledger.new_draft('receipt', payload)
    assert ledger.confirm_receipt(draft, payload)['necessity'] == 'necessity'


def test_income_api_auth_and_crud(ledger, monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD', 'test-income-password')
    headers = {'X-Penny-Client': 'dashboard'}
    with TestClient(create_app(ledger)) as client:
        payload = {'source': 'Salary', 'amount': '3000'}
        assert client.post('/api/income', headers=headers, json=payload).status_code == 401
        client.post('/api/login', headers=headers, json={'password': 'test-income-password'})
        created = client.post('/api/income', headers=headers, json=payload)
        assert created.status_code == 201
        identifier = created.json()['id']
        assert client.put(f'/api/income/{identifier}', headers=headers, json={**payload, 'amount': '3100'}).status_code == 200
        assert client.get('/api/summary').json()['income_total'] == 310000
        assert client.delete(f'/api/income/{identifier}', headers=headers).status_code == 200
        assert client.get('/api/summary').json()['income_total'] == 0
