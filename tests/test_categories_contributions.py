from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest

from app import core
from app.bot import summary_text
from app.core import CATEGORIES, Ledger, category, parse_add
from app.transfers import export_csv
from app.receipts import parse_receipt


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 21))
    return Ledger(tmp_path / 'ledger.sqlite3')


def test_exact_eight_categories_and_legacy_aliases(ledger):
    assert [c['label'] for c in ledger.summary()['categories']] == [
        'Food & drinks', 'Transport', 'Home & bills', 'Health & protection',
        'Shopping & personal', 'Leisure & social', 'Travel', 'Learning & other']
    assert len(CATEGORIES) == 8
    for old, new in [('insurance', 'health'), ('fitness', 'health'), ('personal_care', 'shopping'),
                     ('entertainment', 'social'), ('gifts', 'social'), ('education', 'other'),
                     ('subscription', 'home'), ('Home & bills', 'home')]:
        assert category(old) == new
    assert parse_add('5 personal-care Haircut')['category'] == 'shopping'
    assert parse_receipt('Salon\nTOTAL SGD 20.00')['category'] == 'shopping'
    assert {r['note']: r['category'] for r in ledger.summary()['expenses']} == {
        'Gym membership': 'health', 'Phone plan': 'home', 'Netflix': 'social'}


def test_contribution_dates_and_separate_investment_accounting(ledger, monkeypatch):
    summary = ledger.summary()
    plans = {s['name']: s for s in summary['subscriptions']}
    household = plans['Household allowance contribution']
    investment = plans['Recurring investments']
    assert (household['amount'], household['billing_day'], household['category']) == (40120, 25, 'home')
    assert (investment['amount'], investment['billing_day'], investment['payment_type']) == (60000, 4, 'investment')
    assert summary['investment_total'] == 60000
    assert summary['investment_contributions'][0]['invested_on'] == '2026-11-04'
    assert summary['monthly_investment_commitment'] == 60000
    assert summary['subscription_total'] == 12410 + 40120
    assert summary['total'] == summary['comparison']['current_total'] == 12410
    assert sum(c['amount'] for c in summary['categories']) == 12410
    assert 'Invested separately: S$600.00' in summary_text(summary)
    assert 'Recurring investments' not in export_csv(ledger)
    ledger.set_budget('2026-11', '1000')
    assert ledger.summary()['remaining'] == 100000 - 12410
    monkeypatch.setattr(core, 'today', lambda: date(2026, 11, 25))
    summary = ledger.summary()
    assert summary['total'] == 12410 + 40120
    assert next(r['spent_on'] for r in summary['expenses'] if r['note'] == household['name']) == '2026-11-25'
    assert summary['remaining'] == 100000 - 12410 - 40120
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 4))
    summary = ledger.summary()
    assert summary['total'] == 12410
    assert summary['investment_total'] == 60000
    assert summary['investment_contributions'][0]['invested_on'] == '2026-12-04'
    assert ledger.summary('2026-10')['investment_total'] == 0


def test_contributions_idempotent_and_edits_survive_restarts(ledger, monkeypatch):
    summary = ledger.summary()
    sub = next(s for s in summary['subscriptions'] if s['payment_type'] == 'investment')
    ledger.save_subscription(sub['id'], {'name': 'Updated investments', 'amount': '650'})
    reopened = Ledger(ledger.path)
    assert len(reopened.summary()['subscriptions']) == 5
    assert reopened.summary()['investment_total'] == 60000
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 4))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Ledger(ledger.path).sync_subscriptions(), range(8)))
    assert reopened.summary()['investment_total'] == 65000
    assert len(reopened.summary()['investment_contributions']) == 1
    ledger.save_subscription(sub['id'], {'name': 'Updated investments', 'amount': '650', 'active': False})
    monkeypatch.setattr(core, 'today', lambda: date(2027, 1, 4))
    assert Ledger(ledger.path).summary()['investment_total'] == 0
    assert Ledger(ledger.path).summary()['monthly_investment_commitment'] == 0


def test_category_migration_preserves_expenses_keys_and_budget_history(ledger):
    # Simulate the category data present immediately before upgrading.
    ledger.sync_subscriptions()
    with ledger.db() as db:
        db.execute("DELETE FROM meta WHERE key='eight_categories_v1'")
        db.execute("UPDATE expenses SET category='personal_care' WHERE note='Phone plan'")
        db.execute("UPDATE expenses SET category='subscription',deleted=1 WHERE note='Netflix'")
        db.execute("UPDATE subscriptions SET category='insurance' WHERE name='Phone plan'")
        db.executemany('INSERT INTO category_budgets VALUES (?,?,?)', [
            ('2026-09', 'health', 10000), ('2026-09', 'insurance', 20000),
            ('2026-10', 'health', 15000), ('2026-11', 'insurance', None)])
        before = [dict(r) for r in db.execute('SELECT * FROM expenses ORDER BY id')]
    updated = Ledger(ledger.path)
    with updated.db() as db:
        after = [dict(r) for r in db.execute('SELECT * FROM expenses ORDER BY id')]
    for old, new in zip(before, after):
        assert {k:v for k,v in old.items() if k != 'category'} == {k:v for k,v in new.items() if k != 'category'}
    assert [r['category'] for r in after] == ['health', 'shopping', 'social']
    assert updated.category_limits('2026-09')['health'] == 30000
    assert updated.category_limits('2026-10')['health'] == 35000
    assert updated.category_limits('2026-11')['health'] == 15000
    assert Ledger(ledger.path).category_limits('2026-11')['health'] == 15000
    assert updated.summary()['count'] == 2


def test_pending_drafts_require_new_review_after_category_migration(ledger):
    row = next(r for r in ledger.summary()['expenses'] if r['note'] == 'Netflix')
    with ledger.db() as db:
        db.execute("UPDATE expenses SET category='subscription' WHERE id=?", (row['id'],))
    proposal = ledger.propose_expense('delete', None, 123, 123, 0, 456, identifier=row['id'])
    csv = ledger.new_draft('csv', {'rows': [{'payload': {'category': 'personal_care'}}]})
    with ledger.db() as db:
        db.execute("DELETE FROM meta WHERE key='eight_categories_v1'")
    updated = Ledger(ledger.path)
    draft = updated.draft(proposal)
    assert draft['payload']['category'] == draft['payload']['_before']['category'] == 'social'
    assert draft['version'] == 2
    with pytest.raises(ValueError, match='outdated'):
        updated.confirm_expense_proposal(proposal, 123, 123, 0, 1)
    assert updated.draft(csv)['status'] == 'cancelled'
    updated.confirm_expense_proposal(proposal, 123, 123, 0, 2)
    assert updated.summary()['count'] == 2


def test_investment_type_cannot_move_recorded_money_between_ledgers(ledger):
    sub = next(s for s in ledger.summary()['subscriptions'] if s['payment_type'] == 'investment')
    with pytest.raises(ValueError, match='new recurring payment'):
        ledger.save_subscription(sub['id'], {'name': sub['name'], 'amount': '600', 'payment_type': 'expense'})
    assert ledger.summary()['investment_total'] == 60000
    assert ledger.summary()['total'] == 12410


def test_investment_cadence_edit_does_not_double_post_same_day(ledger, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 4))
    summary = ledger.summary()
    sub = next(s for s in summary['subscriptions'] if s['payment_type'] == 'investment')
    assert summary['investment_total'] == 60000
    ledger.save_subscription(sub['id'], {'name': sub['name'], 'amount': '600',
                                         'interval_unit': 'weeks', 'start_date': '2026-12-04'})
    assert ledger.summary()['investment_total'] == 60000
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 11))
    assert ledger.summary()['investment_total'] == 120000


def test_investment_reminder_uses_4th_and_persists_delivery(ledger, monkeypatch):
    monkeypatch.setattr(core, 'today', lambda: date(2026, 12, 1))
    now = datetime(2026, 12, 1, 9, tzinfo=core.ZONE)
    reminders = ledger.pending_notifications(now)
    reminder = next(n for n in reminders if 'Recurring investments' in n['text'])
    assert 'investment contribution' in reminder['text'] and '2026-12-04' in reminder['text']
    assert ledger.summary()['investment_total'] == 0
    ledger.mark_notification(reminder['keys'], now)
    assert not any('Recurring investments' in n['text'] for n in Ledger(ledger.path).pending_notifications(now))
