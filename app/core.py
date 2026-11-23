"""Shared ledger rules for the web app and Telegram worker. Money is integer cents."""
import calendar
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .features import FeatureMixin, currency_fields
from .telegram_ledger import TelegramLedgerMixin
from .recurrence import annual_cost, due_dates, start_date
from .categories import CATEGORIES, ALIASES, migrate_categories
from .contributions import initialize_contributions
from .cashflow import CashflowMixin, priority, resolved_priority

ZONE = ZoneInfo(os.getenv('TZ', 'Asia/Singapore'))


def today():
    return datetime.now(ZONE).date()


def month_bounds(month):
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('Use a month in YYYY-MM format.')
    try:
        first = date.fromisoformat(month + '-01')
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    except ValueError:
        raise ValueError('Invalid month.') from None
    return first, last


def cents(value):
    """Reject excess precision instead of silently rounding a financial entry."""
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > Decimal('9999999.99'):
            raise ValueError
        minor = number * 100
        if minor != minor.to_integral_value():
            raise ValueError
        return int(minor)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Enter an amount from 0.01 to 9,999,999.99 with at most two decimal places.') from None


def category(value):
    value = ALIASES.get(str(value).lower(), str(value).lower())
    if value not in CATEGORIES:
        raise ValueError('Unknown category. Use /categories to see the list.')
    return value


def money(value):
    return f'S${value / 100:,.2f}'


class Ledger(CashflowMixin, FeatureMixin, TelegramLedgerMixin):
    def __init__(self, path=None):
        self.path = Path(path or Path(os.getenv('DATA_DIR', 'data')) / 'ledger.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=30000')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount>0),
                category TEXT NOT NULL, note TEXT NOT NULL, spent_on TEXT NOT NULL,
                source TEXT NOT NULL, source_key TEXT UNIQUE,
                deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
              CREATE INDEX IF NOT EXISTS expense_dates ON expenses(spent_on, deleted);
              CREATE TABLE IF NOT EXISTS budgets (month TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount>0));
              CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
                billing_day INTEGER NOT NULL CHECK(billing_day BETWEEN 1 AND 31),
                active INTEGER NOT NULL DEFAULT 1, start_month TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS recaps (month TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
            ''')
            # BEGIN IMMEDIATE also serializes simultaneous first starts of web and bot.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(subscriptions)')}
            for name, definition in (('interval_count', 'INTEGER NOT NULL DEFAULT 1'),
                                     ('interval_unit', "TEXT NOT NULL DEFAULT 'months'"),
                                     ('category', "TEXT NOT NULL DEFAULT 'home'"),
                                     ('payment_type', "TEXT NOT NULL DEFAULT 'expense'"),
                                     ('start_date', 'TEXT'), ('schedule_effective_from', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE subscriptions ADD COLUMN {name} {definition}')
            if 'category' not in columns:
                # Old subscriptions had no category field; classify their known names.
                db.execute("UPDATE subscriptions SET category='subscription'")
            self.initialize_features(db)
            self.initialize_telegram(db)
            if not db.execute("SELECT 1 FROM meta WHERE key='start_month'").fetchone():
                start = today().strftime('%Y-%m')
                db.execute("INSERT INTO meta VALUES ('start_month',?)", (start,))
                db.executemany('INSERT INTO subscriptions(name,amount,billing_day,start_month) VALUES (?,?,1,?)',
                               [('Gym membership', 9800, start), ('Phone plan', 1010, start), ('Netflix', 1600, start)])
                db.execute("UPDATE subscriptions SET category='subscription'")
            migrate_categories(db)
            initialize_contributions(db, today())
            self.initialize_cashflow(db)

    def sync_subscriptions(self, as_of=None):
        as_of = as_of or today()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._sync_subscriptions(db, as_of)

    def _sync_subscriptions(self, db, as_of):
        for sub in db.execute('SELECT * FROM subscriptions WHERE active=1').fetchall():
            for due in due_dates(sub, start_date(sub), as_of):
                period = due.isoformat() if sub['interval_unit'] in ('days', 'weeks') else due.strftime('%Y-%m')
                if sub['payment_type'] == 'income':
                    db.execute('''INSERT OR IGNORE INTO income_entries(source,amount,received_on,source_key)
                        SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM income_entries WHERE source_key LIKE ? AND received_on=?)''',
                        (sub['name'], sub['amount'], due.isoformat(), f"income:{sub['id']}:{period}", f"income:{sub['id']}:%", due.isoformat()))
                    continue
                if sub['payment_type'] == 'investment':
                    db.execute('''INSERT OR IGNORE INTO investment_contributions(amount,note,invested_on,source_key)
                        SELECT ?,?,?,? WHERE NOT EXISTS
                        (SELECT 1 FROM investment_contributions WHERE source_key LIKE ? AND invested_on=?)''',
                        (sub['amount'], sub['name'], due.isoformat(), f"investment:{sub['id']}:{period}",
                         f"investment:{sub['id']}:%", due.isoformat()))
                    continue
                # Preserve existing monthly keys, including deleted and edited entries.
                db.execute('''INSERT OR IGNORE INTO expenses(amount,category,note,spent_on,source,source_key,necessity)
                    SELECT ?,?,?,?,'subscription',?,? WHERE NOT EXISTS
                    (SELECT 1 FROM expenses WHERE source_key LIKE ? AND spent_on=?)''',
                    (sub['amount'], sub['category'], sub['name'], due.isoformat(), f"subscription:{sub['id']}:{period}", resolved_priority(dict(sub)),
                     f"subscription:{sub['id']}:%", due.isoformat()))

    def validate_expense(self, payload):
        priority(payload)
        amount = cents(payload.get('amount'))
        cat = category(payload.get('category'))
        note = payload.get('note', '')
        if not isinstance(note, str) or len(note.strip()) > 240:
            raise ValueError('Description must be at most 240 characters.')
        try:
            spent_on = date.fromisoformat(payload.get('spent_on') or today().isoformat())
        except (ValueError, TypeError):
            raise ValueError('Use a valid expense date in YYYY-MM-DD format.') from None
        if spent_on > today():
            raise ValueError('An expense cannot be dated in the future.')
        return amount, cat, note.strip(), spent_on.isoformat()

    def add_expense(self, payload, source='dashboard', source_key=None):
        values = self.validate_expense(payload)
        currency = currency_fields(payload)
        necessity = priority(payload)
        with self.db() as db:
            db.execute('''INSERT OR IGNORE INTO expenses(amount,category,note,spent_on,source,source_key,original_currency,original_amount,necessity)
                       VALUES (?,?,?,?,?,?,?,?,?)''', (*values, source, source_key, *currency, necessity))
            if source_key:
                return dict(db.execute('SELECT * FROM expenses WHERE source_key=?', (source_key,)).fetchone())
            return dict(db.execute('SELECT * FROM expenses WHERE id=last_insert_rowid()').fetchone())

    def update_expense(self, identifier, payload):
        values = self.validate_expense(payload)
        currency = currency_fields(payload)
        necessity = priority(payload)
        with self.db() as db:
            result = db.execute('UPDATE expenses SET amount=?,category=?,note=?,spent_on=?,original_currency=?,original_amount=?,necessity=CASE WHEN ? THEN ? ELSE necessity END WHERE id=? AND deleted=0', (*values, *currency, 'necessity' in payload, necessity, identifier))
            if not result.rowcount:
                raise LookupError('Expense not found.')

    def delete_expense(self, identifier):
        # Keep its idempotency key so a deleted recurring/bot expense never reappears.
        with self.db() as db:
            result = db.execute('UPDATE expenses SET deleted=1 WHERE id=? AND deleted=0', (identifier,))
            if not result.rowcount:
                raise LookupError('Expense not found.')

    def set_budget(self, month, value):
        month_bounds(month)
        if month > today().strftime('%Y-%m'):
            raise ValueError('Select the current month or an earlier month.')
        amount = cents(value)
        with self.db() as db:
            db.execute('INSERT INTO budgets VALUES (?,?) ON CONFLICT(month) DO UPDATE SET amount=excluded.amount', (month, amount))

    def save_subscription(self, identifier, payload):
        name = payload.get('name', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Subscription name must be between 1 and 100 characters.')
        amount = cents(payload.get('amount'))
        day = payload.get('billing_day', 1)
        if type(day) is not int or not 1 <= day <= 31:
            raise ValueError('Billing day must be a whole number from 1 to 31.')
        active = payload.get('active', True)
        if type(active) is not bool:
            raise ValueError('Active must be true or false.')
        start = payload.get('start_month') or today().strftime('%Y-%m')
        month_bounds(start)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM subscriptions WHERE id=?', (identifier,)).fetchone() if identifier is not None else None
            if identifier is not None and old is None:
                raise LookupError('Subscription not found.')
            cat = category(payload.get('category', old['category'] if old else 'home'))
            kind = payload.get('payment_type', old['payment_type'] if old else 'expense')
            if kind not in ('expense', 'investment', 'income'):
                raise ValueError('Payment type must be expense, investment, or income.')
            necessity = priority(payload)
            if old and kind != old['payment_type']:
                raise ValueError('Create a new recurring payment to change its payment type.')
            count = payload.get('interval_count', old['interval_count'] if old else 1)
            unit = payload.get('interval_unit', old['interval_unit'] if old else 'months')
            if type(count) is not int or not 1 <= count <= 120:
                raise ValueError('Billing interval must be a whole number from 1 to 120.')
            if not isinstance(unit, str) or unit not in ('days', 'weeks', 'months', 'years'):
                raise ValueError('Billing interval unit must be days, weeks, months, or years.')
            anchor = start_date(old).isoformat() if old else f'{start}-{min(day, month_bounds(start)[1].day):02d}'
            if 'start_date' in payload:
                try:
                    anchor = date.fromisoformat(payload['start_date']).isoformat()
                except (ValueError, TypeError):
                    raise ValueError('Use a valid first billing date in YYYY-MM-DD format.') from None
                # Keep a legacy day-31 anchor when a form round-trips its clamped date.
                day = old['billing_day'] if old and anchor == start_date(old).isoformat() and 'billing_day' not in payload else date.fromisoformat(anchor).day
            elif old and 'billing_day' not in payload:
                day = old['billing_day']
            changed = old and (count != old['interval_count'] or unit != old['interval_unit'] or
                              anchor != start_date(old).isoformat() or day != old['billing_day'] or
                              (active and not old['active']))
            effective = today().isoformat() if changed else (old['schedule_effective_from'] if old else None)
            # Post old terms and save the new schedule under the same write lock.
            self._sync_subscriptions(db, today())
            if identifier is None:
                db.execute('''INSERT INTO subscriptions(name,amount,billing_day,active,start_month,
                           interval_count,interval_unit,start_date,schedule_effective_from,category,payment_type,necessity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (name.strip(), amount, day, int(active), anchor[:7], count, unit, anchor, effective, cat, kind, necessity))
            else:
                db.execute('''UPDATE subscriptions SET name=?,amount=?,billing_day=?,active=?,interval_count=?,
                           interval_unit=?,start_date=?,schedule_effective_from=?,category=?,necessity=CASE WHEN ? THEN ? ELSE necessity END WHERE id=?''',
                           (name.strip(), amount, day, int(active), count, unit, anchor, effective, cat, 'necessity' in payload, necessity, identifier))
            self._sync_subscriptions(db, today())

    def summary(self, month=None):
        month = month or today().strftime('%Y-%m')
        first, last = month_bounds(month)
        if first > today():
            raise ValueError('Select the current month or an earlier month.')
        self.sync_subscriptions()
        with self.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM expenses WHERE deleted=0 AND spent_on BETWEEN ? AND ? ORDER BY spent_on DESC,id DESC',
                                              (first.isoformat(), last.isoformat()))]
            budget = db.execute('SELECT * FROM budgets WHERE month<=? ORDER BY month DESC LIMIT 1', (month,)).fetchone()
            investments = [dict(r) for r in db.execute('''SELECT * FROM investment_contributions
                WHERE invested_on BETWEEN ? AND ? ORDER BY invested_on DESC,id DESC''', (first.isoformat(), last.isoformat()))]
            subs = [dict(r) for r in db.execute('SELECT * FROM subscriptions ORDER BY id')]
            for sub in subs:
                sub['start_date'] = start_date(sub).isoformat()
                sub['necessity'] = resolved_priority(sub)
            start_month = db.execute("SELECT value FROM meta WHERE key='start_month'").fetchone()['value']
        totals = {key: 0 for key in CATEGORIES}
        daily = {day: 0 for day in range(1, last.day + 1)}
        for row in rows:
            row['necessity'] = resolved_priority(row)
            totals[row['category']] += row['amount']
            daily[int(row['spent_on'][-2:])] += row['amount']
        total = sum(totals.values())
        elapsed = min(today(), last).day
        return {'month': month, 'today': today().isoformat(), 'timezone': str(ZONE), 'currency': 'SGD',
                'total': total, 'count': len(rows), 'budget': budget['amount'] if budget else None,
                'budget_from': budget['month'] if budget else None,
                'remaining': budget['amount'] - total if budget else None,
                'daily_average': round(total / elapsed), 'elapsed_days': elapsed, 'days_in_month': last.day,
                'categories': [{'id': key, 'label': label, 'color': color, 'amount': totals[key]} for key, (label, color) in CATEGORIES.items()],
                'daily': [{'day': day, 'amount': value} for day, value in daily.items()],
                'expenses': rows, 'subscriptions': subs, 'start_month': start_month,
                'subscription_total': round(sum(annual_cost(s) for s in subs if s['active'] and s['payment_type'] == 'expense') / 12),
                'investment_total': sum(r['amount'] for r in investments), 'investment_contributions': investments,
                'monthly_investment_commitment': round(sum(annual_cost(s) for s in subs if s['active'] and s['payment_type'] == 'investment') / 12),
                **self.cashflow_summary(first, last, rows, sum(r['amount'] for r in investments)),
                **self.feature_summary(month)}

    def get_state(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM bot_state WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_state(self, key, value):
        with self.db() as db:
            db.execute('INSERT INTO bot_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def parse_add(text, as_of=None):
    """/add 12.50 food Lunch @2026-11-20. The date suffix is optional."""
    original = {}
    match = re.search(r'\s+--original\s+([A-Za-z]{3})\s+(\S+)\s*$', text)
    if match:
        original = {'original_currency': match[1].upper(), 'original_amount': match[2]}
        currency_fields(original)
        text = text[:match.start()].strip()
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError('Use /add 12.50 food Lunch. Optional date: /add 12.50 food Lunch @YYYY-MM-DD')
    amount = parts[0].removeprefix('S$').removeprefix('$')
    cents(amount)
    cat = category(parts[1])
    note = parts[2] if len(parts) > 2 else ''
    spent_on = (as_of or today()).isoformat()
    suffix = re.search(r'(?:^|\s)@(\S+)$', note)
    if suffix:
        try:
            spent_on = date.fromisoformat(suffix[1]).isoformat()
        except ValueError:
            raise ValueError('Use a valid date after @, for example @2026-11-20.') from None
        note = note[:suffix.start()].strip()
    return {'amount': amount, 'category': cat, 'note': note, 'spent_on': spent_on, **original}
