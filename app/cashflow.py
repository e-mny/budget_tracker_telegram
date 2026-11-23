"""Recorded income and the user's own spending priorities."""
import re

PRIORITIES = ('necessity', 'non_necessity', 'unclassified')


def priority(payload):
    value = payload.get('necessity')
    if value is not None and value not in PRIORITIES:
        raise ValueError('Choose necessity, non_necessity, or unclassified.')
    return value


def resolved_priority(row):
    if row['necessity'] is not None:
        return row['necessity']
    description = row.get('note', row.get('name', ''))
    return 'necessity' if re.search(r'\b(gym|insurance|phone|household)\b', description, re.I) else 'unclassified'


class CashflowMixin:
    def initialize_cashflow(self, db):
        for table in ('expenses', 'subscriptions'):
            columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            if 'necessity' not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN necessity TEXT')
        db.execute('''CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
            received_on TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', source_key TEXT UNIQUE,
            deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        db.execute('CREATE INDEX IF NOT EXISTS income_dates ON income_entries(received_on,deleted)')

    def save_income(self, identifier, payload):
        from .core import cents, today
        from datetime import date
        source, note = payload.get('source', ''), payload.get('note', '')
        if not isinstance(source, str) or not 1 <= len(source.strip()) <= 100:
            raise ValueError('Income source must be between 1 and 100 characters.')
        if not isinstance(note, str) or len(note.strip()) > 240:
            raise ValueError('Description must be at most 240 characters.')
        amount = cents(payload.get('amount'))
        try:
            received = date.fromisoformat(payload.get('received_on') or today().isoformat())
        except (ValueError, TypeError):
            raise ValueError('Use a valid income date in YYYY-MM-DD format.') from None
        if received > today():
            raise ValueError('Income cannot be dated in the future. Use a recurring payment for scheduled income.')
        with self.db() as db:
            values = (source.strip(), amount, received.isoformat(), note.strip())
            if identifier is None:
                return db.execute('INSERT INTO income_entries(source,amount,received_on,note) VALUES (?,?,?,?)', values).lastrowid
            if not db.execute('UPDATE income_entries SET source=?,amount=?,received_on=?,note=? WHERE id=? AND deleted=0', (*values, identifier)).rowcount:
                raise LookupError('Income entry not found.')
            return identifier

    def delete_income(self, identifier):
        with self.db() as db:
            if not db.execute('UPDATE income_entries SET deleted=1 WHERE id=? AND deleted=0', (identifier,)).rowcount:
                raise LookupError('Income entry not found.')

    def cashflow_summary(self, first, last, expenses, investment_total):
        with self.db() as db:
            incomes = [dict(r) for r in db.execute('''SELECT * FROM income_entries WHERE deleted=0
                AND received_on BETWEEN ? AND ? ORDER BY received_on DESC,id DESC''', (first.isoformat(), last.isoformat()))]
        sources = {}
        for row in incomes:
            sources[row['source']] = sources.get(row['source'], 0) + row['amount']
        spending = {key: 0 for key in PRIORITIES}
        for row in expenses:
            spending[row['necessity']] += row['amount']
        income = sum(sources.values())
        return {'income_entries': incomes, 'income_sources': [{'name': k, 'amount': v} for k, v in sorted(sources.items())],
                'income_total': income, 'spending_priorities': spending,
                'net_cashflow': income - sum(spending.values()) - investment_total}
