from datetime import date

import pytest

from app import core
from app.core import Ledger
from app.categories import CATEGORIES
from app.recurrence import monthly_breakdown


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 23))
    return Ledger(tmp_path / 'ledger.sqlite3')


def by_category(result):
    return {row['id']: row for row in result['recurring_category_breakdown']}


def test_yearly_and_quarterly_costs_appear_in_their_categories(ledger):
    ledger.set_budget('2026-11', '1000')
    ledger.save_subscription(None, {'name': 'Annual insurance', 'amount': '120', 'category': 'health',
                                   'interval_unit': 'years', 'start_date': '2026-11-30'})
    ledger.save_subscription(None, {'name': 'Quarterly streaming', 'amount': '90', 'category': 'social',
                                   'interval_count': 3, 'start_date': '2026-12-01'})
    result = ledger.summary()
    categories = by_category(result)
    assert categories['health']['monthly_amount'] == 9800 + 1000
    assert categories['social']['monthly_amount'] == 1600 + 3000
    assert next(p['monthly_amount'] for p in categories['health']['plans'] if p['name'] == 'Annual insurance') == 1000
    assert result['subscription_total'] == 52530 + 4000
    assert result['total'] == 12410
    assert result['remaining'] == 100000 - 12410
    assert result['net_cashflow'] == -12410 - 60000
    assert sum(r['monthly_amount'] for r in categories.values()) == result['subscription_total']


def test_full_renewal_charge_does_not_double_count_monthly_equivalent(ledger, monkeypatch):
    ledger.save_subscription(None, {'name': 'Annual insurance', 'amount': '120', 'category': 'health',
                                   'interval_unit': 'years', 'start_date': '2026-11-30'})
    before = ledger.summary()
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 30))
    after = ledger.summary()
    assert by_category(after)['health']['monthly_amount'] == by_category(before)['health']['monthly_amount'] == 10800
    assert next(c['amount'] for c in after['categories'] if c['id'] == 'health') == 9800 + 12000
    assert after['total'] == 12410 + 40120 + 12000


def test_excludes_income_investments_and_paused_plans(ledger):
    ledger.save_subscription(None, {'name': 'Salary', 'amount': '3000', 'payment_type': 'income', 'start_date': '2026-12-01'})
    ledger.save_subscription(None, {'name': 'Paused insurance', 'amount': '120', 'category': 'health',
                                   'interval_unit': 'years', 'active': False})
    result = ledger.summary()
    names = [p['name'] for c in result['recurring_category_breakdown'] for p in c['plans']]
    assert 'Salary' not in names and 'Recurring investments' not in names and 'Paused insurance' not in names
    assert result['subscription_total'] == 52530


def test_current_plans_are_consistent_when_browsing_historical_months(ledger):
    ledger.save_subscription(None, {'name': 'Annual insurance', 'amount': '120', 'category': 'health',
                                   'interval_unit': 'years', 'start_date': '2027-02-01'})
    current = ledger.summary()
    past = ledger.summary('2026-10')
    assert past['recurring_category_breakdown'] == current['recurring_category_breakdown']
    assert past['total'] == 0
    sub = next(s for s in current['subscriptions'] if s['name'] == 'Annual insurance')
    ledger.save_subscription(sub['id'], {'name': sub['name'], 'amount': '240', 'category': 'home'})
    updated = by_category(ledger.summary())
    assert updated['health']['monthly_amount'] == 9800
    assert updated['home']['monthly_amount'] == 41130 + 2000


def test_rounding_reconciles_plans_categories_and_total():
    plans = [{'id': i, 'name': str(i), 'amount': 100, 'active': True, 'payment_type': 'expense',
              'interval_unit': 'years', 'interval_count': 1, 'category': cat}
             for i, cat in enumerate(('food', 'health', 'home'), start=1)]
    rows, total = monthly_breakdown(plans, CATEGORIES)
    assert total == 25
    assert sum(row['monthly_amount'] for row in rows) == total
    assert all(sum(p['monthly_amount'] for p in row['plans']) == row['monthly_amount'] for row in rows)
    assert [row['monthly_amount'] for row in rows if row['plans']] == [9, 8, 8]
    empty, zero = monthly_breakdown([], CATEGORIES)
    assert zero == 0 and all(row['monthly_amount'] == 0 for row in empty)
