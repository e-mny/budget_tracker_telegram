"""Budgets, goals, comparison views, and durable, configurable notifications."""
import hashlib
import json
import re
import secrets
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from .recurrence import annual_cost, next_due
from .cashflow import priority

CURRENCY_DIGITS = dict.fromkeys('SGD USD EUR GBP MYR THB IDR AUD NZD CNY HKD TWD PHP INR CHF CAD AED'.split(), 2)
CURRENCY_DIGITS.update(dict.fromkeys('JPY KRW VND'.split(), 0))
CURRENCY_DIGITS.update(dict.fromkeys('KWD BHD OMR'.split(), 3))


def currency_fields(payload):
    code = payload.get('original_currency') or None
    value = payload.get('original_amount')
    if code is None and value in (None, ''):
        return None, None
    if not isinstance(code, str) or code.upper() not in CURRENCY_DIGITS:
        raise ValueError('Choose a supported original currency.')
    code = code.upper()
    try:
        amount = Decimal(str(value))
        digits = CURRENCY_DIGITS[code]
        if not amount.is_finite() or amount <= 0 or amount > Decimal('999999999999'):
            raise ValueError
        scaled = amount * (10 ** digits)
        if scaled != scaled.to_integral_value():
            raise ValueError
        return code, f'{amount:.{digits}f}'
    except (ValueError, InvalidOperation):
        raise ValueError(f'Enter a positive original amount with at most {CURRENCY_DIGITS[code]} decimal places for {code}.') from None


class FeatureMixin:
    def initialize_features(self, db):
        # All migrations run while Ledger.initialize holds BEGIN IMMEDIATE.
        columns = {row['name'] for row in db.execute('PRAGMA table_info(expenses)')}
        for name in ('original_currency', 'original_amount'):
            if name not in columns:
                db.execute(f'ALTER TABLE expenses ADD COLUMN {name} TEXT')
        statements = [
            'CREATE TABLE IF NOT EXISTS category_budgets(month TEXT NOT NULL, category TEXT NOT NULL, amount INTEGER, PRIMARY KEY(month,category))',
            'CREATE TABLE IF NOT EXISTS goals(id INTEGER PRIMARY KEY,name TEXT NOT NULL,target INTEGER NOT NULL,saved INTEGER NOT NULL DEFAULT 0,target_date TEXT)',
            'CREATE TABLE IF NOT EXISTS goal_contributions(key TEXT PRIMARY KEY,goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,amount INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS notification_log(key TEXT PRIMARY KEY,sent_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,expires INTEGER NOT NULL,status TEXT NOT NULL DEFAULT \'pending\',source_key TEXT UNIQUE)',
        ]
        for statement in statements:
            db.execute(statement)
        db.execute("INSERT OR IGNORE INTO meta VALUES ('notification_settings',?)", (json.dumps({
            'budget_alerts': True, 'thresholds': [50,80,100], 'renewal_reminders': True, 'reminder_days': 3}),))

    def settings(self):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='notification_settings'").fetchone()
        return json.loads(row['value'])

    def set_settings(self, payload):
        values = self.settings()
        for key in ('budget_alerts', 'renewal_reminders'):
            if key in payload:
                if type(payload[key]) is not bool:
                    raise ValueError('Notification switches must be true or false.')
                values[key] = payload[key]
        thresholds = payload.get('thresholds', values['thresholds'])
        if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 10 or any(type(t) is not int or not 1 <= t <= 100 for t in thresholds):
            raise ValueError('Choose 1–10 whole-number thresholds between 1 and 100 percent.')
        days = payload.get('reminder_days', values['reminder_days'])
        if type(days) is not int or not 0 <= days <= 14:
            raise ValueError('Reminder lead time must be from 0 to 14 days.')
        values.update(thresholds=sorted(set(thresholds)), reminder_days=days)
        with self.db() as db:
            db.execute("UPDATE meta SET value=? WHERE key='notification_settings'", (json.dumps(values),))
        return values

    def set_category_budget(self, month, cat, value):
        from .core import category, cents, month_bounds, today
        month_bounds(month)
        cat = category(cat)
        if month > today().strftime('%Y-%m'):
            raise ValueError('Select the current month or an earlier month.')
        amount = None if value in (None, '') else cents(value)
        with self.db() as db:
            db.execute('INSERT INTO category_budgets VALUES (?,?,?) ON CONFLICT(month,category) DO UPDATE SET amount=excluded.amount', (month, cat, amount))

    def category_limits(self, month):
        with self.db() as db:
            rows = db.execute('''SELECT b.* FROM category_budgets b WHERE b.month=(SELECT MAX(x.month)
                 FROM category_budgets x WHERE x.category=b.category AND x.month<=?)''', (month,)).fetchall()
        return {row['category']: row['amount'] for row in rows}

    def comparison(self, month):
        from .core import CATEGORIES, month_bounds, today
        first, last = month_bounds(month)
        previous_last = first - timedelta(days=1)
        previous_first = previous_last.replace(day=1)
        elapsed = min(today(), last).day
        is_partial = today() < last and today().strftime('%Y-%m') == month
        previous_cutoff = previous_first.replace(day=min(elapsed, previous_last.day)) if is_partial else previous_last
        with self.db() as db:
            def totals(start, end):
                return {r['category']: r['amount'] for r in db.execute('SELECT category,SUM(amount) amount FROM expenses WHERE deleted=0 AND spent_on BETWEEN ? AND ? GROUP BY category', (start.isoformat(),end.isoformat()))}
            current = totals(first, min(today(), last))
            previous = totals(previous_first, previous_cutoff)
            full_previous = totals(previous_first, previous_last)
            recorded_before = db.execute('SELECT MIN(spent_on) oldest FROM expenses WHERE deleted=0').fetchone()['oldest']
            start = db.execute("SELECT value FROM meta WHERE key='start_month'").fetchone()['value']
        old, new = sum(previous.values()), sum(current.values())
        has_history = previous_last.strftime('%Y-%m') >= start or (recorded_before is not None and recorded_before <= previous_last.isoformat())
        return {'previous_month': previous_first.strftime('%Y-%m'), 'current_total': new, 'previous_total': old,
                'previous_full_total': sum(full_previous.values()), 'change': new-old,
                'change_percent': round((new-old)/old*100,1) if old else None,
                'partial': is_partial, 'previous_through': previous_cutoff.isoformat(), 'has_history': has_history,
                'categories': [{'id': key,'label': label,'color': color,'current': current.get(key,0),'previous': previous.get(key,0)} for key,(label,color) in CATEGORIES.items()]}

    def save_goal(self, identifier, payload):
        from .core import cents
        name = payload.get('name', '')
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Goal name must be between 1 and 100 characters.')
        target = cents(payload.get('target'))
        raw_saved = payload.get('saved', '0')
        try:
            saved = 0 if Decimal(str(raw_saved)) == 0 else cents(raw_saved)
        except InvalidOperation:
            raise ValueError('Enter a valid already-saved amount, including zero.') from None
        due = payload.get('target_date') or None
        if due:
            try:
                due = date.fromisoformat(due).isoformat()
            except (ValueError,TypeError):
                raise ValueError('Use a valid target date.') from None
        with self.db() as db:
            if identifier is None:
                result = db.execute('INSERT INTO goals(name,target,saved,target_date) VALUES (?,?,?,?)',(name.strip(),target,saved,due))
                return result.lastrowid
            result = db.execute('UPDATE goals SET name=?,target=?,saved=?,target_date=? WHERE id=?',(name.strip(),target,saved,due,identifier))
            if not result.rowcount:
                raise LookupError('Savings goal not found.')
            return identifier

    def contribute_goal(self, identifier, value, key):
        from .core import cents
        amount = cents(value)
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):
            raise ValueError('A valid contribution request key is required.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM goals WHERE id=?',(identifier,)).fetchone():
                raise LookupError('Savings goal not found.')
            existing = db.execute('SELECT * FROM goal_contributions WHERE key=?',(key,)).fetchone()
            if existing:
                if existing['goal_id'] != identifier or existing['amount'] != amount:
                    raise ValueError('This request key has already been used for a different contribution.')
                return
            db.execute('INSERT INTO goal_contributions VALUES (?,?,?)',(key,identifier,amount))
            db.execute('UPDATE goals SET saved=saved+? WHERE id=?',(amount,identifier))

    def delete_goal(self, identifier):
        with self.db() as db:
            if not db.execute('DELETE FROM goals WHERE id=?',(identifier,)).rowcount:
                raise LookupError('Savings goal not found.')

    def feature_summary(self, month):
        from .core import today
        with self.db() as db:
            goals = [dict(r) for r in db.execute('SELECT * FROM goals ORDER BY id')]
            subs = [dict(r) for r in db.execute('SELECT * FROM subscriptions WHERE active=1')]
        limits = self.category_limits(month)
        for sub in subs:
            due = next_due(sub, today())
            sub['next_due'] = due.isoformat() if due else None
        return {'category_budgets': limits,'goals': goals,'comparison': self.comparison(month),
                'notification_settings': self.settings(),'upcoming_subscriptions': subs,
                'annual_subscription_total': round(sum(annual_cost(s) for s in subs if s['payment_type'] == 'expense')),
                'currencies': list(CURRENCY_DIGITS)}

    def pending_notifications(self, now):
        from .core import money
        month = now.strftime('%Y-%m')
        summary = self.summary(month)
        settings = self.settings()
        with self.db() as db:
            sent = {r['key'] for r in db.execute('SELECT key FROM notification_log')}
        result = []
        if settings['budget_alerts']:
            budgets = [('total','Monthly budget',summary['total'],summary['budget'])]
            budgets += [(c['id'],c['label'],c['amount'],summary['category_budgets'].get(c['id'])) for c in summary['categories']]
            for cat,label,spent,budget in budgets:
                if not budget:
                    continue
                crossed = [t for t in settings['thresholds'] if spent*100 >= budget*t]
                pending = [t for t in crossed if f'budget:{month}:{cat}:{t}' not in sent]
                if pending:
                    result.append({'keys':[f'budget:{month}:{cat}:{t}' for t in crossed],
                                   'text':f'Penny · {label} alert ({month})\nYou have reached {max(crossed)}% of your limit.\nSpent {money(spent)} of {money(budget)}.\n'+
                                   (f'Over budget by {money(spent-budget)}.' if spent>budget else f'Remaining: {money(budget-spent)}.')})
        if settings['renewal_reminders'] and now.hour >= 9:
            for sub in summary['upcoming_subscriptions']:
                due = sub.get('next_due')
                if not due:
                    continue
                due_date = date.fromisoformat(due)
                if not now.date() <= due_date <= now.date()+timedelta(days=settings['reminder_days']):
                    continue
                key = f"renewal:{sub['id']}:{due}"
                if key not in sent:
                    title = {'investment': 'investment contribution', 'income': 'income payment'}.get(sub['payment_type'], 'subscription renewal')
                    result.append({'keys':[key], 'text':f"Penny · {title}\n{sub['name']} · {money(sub['amount'])}\nDue {due}. Check your configured billing schedule in the dashboard."})
        return result

    def mark_notification(self, keys, now):
        with self.db() as db:
            db.executemany('INSERT OR IGNORE INTO notification_log VALUES (?,?)',[(key,now.isoformat()) for key in keys])

    def new_draft(self, kind, payload, source_key=None):
        identifier = secrets.token_hex(12)
        with self.db() as db:
            db.execute("DELETE FROM drafts WHERE expires<?", (int(time.time()),))
            if source_key:
                old = db.execute('SELECT id FROM drafts WHERE source_key=?',(source_key,)).fetchone()
                if old:
                    return old['id']
            db.execute('INSERT INTO drafts(id,kind,payload,expires,source_key) VALUES (?,?,?,?,?)',
                       (identifier,kind,json.dumps(payload),int(time.time())+7*86400,source_key))
        return identifier

    def draft(self, identifier, kind=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM drafts WHERE id=? AND expires>?',(identifier,int(time.time()))).fetchone()
        if not row or (kind and row['kind']!=kind):
            raise LookupError('Draft not found or expired. Start a new request.')
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result

    def cancel_draft(self, identifier):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM drafts WHERE id=? AND expires>?',(identifier,int(time.time()))).fetchone()
            if not row:
                raise LookupError('Draft not found or expired.')
            if row['status'] == 'saved':
                raise ValueError('This draft was already saved. Use /edit or /delete in Telegram, or edit it in the dashboard.')
            db.execute("UPDATE drafts SET status='cancelled' WHERE id=? AND status='pending'",(identifier,))

    def confirm_receipt(self, identifier, payload, expected_version=None):
        self.validate_expense(payload)
        currency_fields(payload)
        # Expense insert and draft consumption share a transaction: cancellation/retry cannot race saving.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM drafts WHERE id=? AND kind=\'receipt\' AND expires>?',(identifier,int(time.time()))).fetchone()
            if not row:
                raise LookupError('Receipt draft not found or expired.')
            if expected_version is not None and row['version']!=expected_version:
                raise ValueError('This preview is outdated. Use the latest receipt confirmation buttons.')
            if row['status']=='cancelled':
                raise ValueError('This receipt draft was cancelled.')
            key = 'receipt:'+identifier
            old = db.execute('SELECT * FROM expenses WHERE source_key=?',(key,)).fetchone()
            if old:
                return dict(old)
            values = self.validate_expense(payload)
            currency = currency_fields(payload)
            cursor = db.execute('''INSERT INTO expenses(amount,category,note,spent_on,source,source_key,original_currency,original_amount,necessity)
                                 VALUES (?,?,?,?,'receipt',?,?,?,?)''',(*values,key,*currency,priority(payload)))
            db.execute("UPDATE drafts SET status='saved' WHERE id=?",(identifier,))
            return dict(db.execute('SELECT * FROM expenses WHERE id=?',(cursor.lastrowid,)).fetchone())
