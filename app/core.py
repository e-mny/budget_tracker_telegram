"""Shared ledger rules for the web app and Telegram worker. Money is integer cents."""
import calendar
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .features import FeatureMixin, currency_fields

ZONE = ZoneInfo(os.getenv('TZ', 'Asia/Singapore'))
CATEGORIES = {
    'food': ('Food & drinks', '#7963ed'),
    'transport': ('Transport', '#38a6ac'),
    'travel': ('Travel', '#5385e8'),
    'shopping': ('Shopping', '#ec9470'),
    'subscription': ('Subscriptions', '#c38adb'),
    'personal_care': ('Personal care', '#d3709c'),
    'health': ('Health & medical', '#4baf8d'),
    'fitness': ('Fitness & sports', '#8fbc52'),
    'entertainment': ('Entertainment', '#e6b34d'),
    'social': ('Social & dates', '#db7c65'),
    'gifts': ('Gifts & donations', '#8b9fdd'),
    'home': ('Home & utilities', '#829399'),
    'education': ('Learning & career', '#63a1cd'),
    'insurance': ('Insurance', '#a293c4'),
    'other': ('Other', '#9ca3af'),
}
ALIASES = {'subscriptions': 'subscription', 'personal-care': 'personal_care',
           'personalcare': 'personal_care', 'dining': 'food', 'groceries': 'food',
           'medical': 'health', 'gym': 'fitness', 'dates': 'social', 'learning': 'education'}


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


class Ledger(FeatureMixin):
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
            self.initialize_features(db)
            if not db.execute("SELECT 1 FROM meta WHERE key='start_month'").fetchone():
                start = today().strftime('%Y-%m')
                db.execute("INSERT INTO meta VALUES ('start_month',?)", (start,))
                db.executemany('INSERT INTO subscriptions(name,amount,billing_day,start_month) VALUES (?,?,1,?)',
                               [('Gym membership', 9800, start), ('Phone plan', 1010, start), ('Netflix', 1600, start)])

    def sync_subscriptions(self, as_of=None):
        as_of = as_of or today()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for sub in db.execute('SELECT * FROM subscriptions WHERE active=1').fetchall():
                cursor, _ = month_bounds(sub['start_month'])
                while cursor <= as_of:
                    due = cursor.replace(day=min(sub['billing_day'], calendar.monthrange(cursor.year, cursor.month)[1]))
                    if due <= as_of:
                        db.execute('''INSERT OR IGNORE INTO expenses(amount,category,note,spent_on,source,source_key)
                            VALUES (?,'subscription',?,?,'subscription',?)''',
                            (sub['amount'], sub['name'], due.isoformat(), f"subscription:{sub['id']}:{cursor:%Y-%m}"))
                    cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    def validate_expense(self, payload):
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
        with self.db() as db:
            db.execute('''INSERT OR IGNORE INTO expenses(amount,category,note,spent_on,source,source_key,original_currency,original_amount)
                       VALUES (?,?,?,?,?,?,?,?)''', (*values, source, source_key, *currency))
            if source_key:
                return dict(db.execute('SELECT * FROM expenses WHERE source_key=?', (source_key,)).fetchone())
            return dict(db.execute('SELECT * FROM expenses WHERE id=last_insert_rowid()').fetchone())

    def update_expense(self, identifier, payload):
        values = self.validate_expense(payload)
        currency = currency_fields(payload)
        with self.db() as db:
            result = db.execute('UPDATE expenses SET amount=?,category=?,note=?,spent_on=?,original_currency=?,original_amount=? WHERE id=? AND deleted=0', (*values, *currency, identifier))
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
        # Materialize any due entries using the old terms before changing future terms.
        self.sync_subscriptions()
        with self.db() as db:
            if identifier is None:
                db.execute('INSERT INTO subscriptions(name,amount,billing_day,active,start_month) VALUES (?,?,?,?,?)',
                           (name.strip(), amount, day, int(active), start))
            else:
                old = db.execute('SELECT * FROM subscriptions WHERE id=?', (identifier,)).fetchone()
                if not old:
                    raise LookupError('Subscription not found.')
                # The original start month remains stable; edits never backdate new charges.
                db.execute('UPDATE subscriptions SET name=?,amount=?,billing_day=?,active=? WHERE id=?',
                           (name.strip(), amount, day, int(active), identifier))
        self.sync_subscriptions()

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
            subs = [dict(r) for r in db.execute('SELECT * FROM subscriptions ORDER BY id')]
            start_month = db.execute("SELECT value FROM meta WHERE key='start_month'").fetchone()['value']
        totals = {key: 0 for key in CATEGORIES}
        daily = {day: 0 for day in range(1, last.day + 1)}
        for row in rows:
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
                'subscription_total': sum(s['amount'] for s in subs if s['active']), **self.feature_summary(month)}

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
