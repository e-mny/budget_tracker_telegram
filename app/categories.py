"""The eight expense categories and migration of the earlier category set."""
import json
from itertools import groupby

CATEGORIES = {
    'food': ('Food & drinks', '#7963ed'),
    'transport': ('Transport', '#38a6ac'),
    'home': ('Home & bills', '#829399'),
    'health': ('Health & protection', '#4baf8d'),
    'shopping': ('Shopping & personal', '#ec9470'),
    'social': ('Leisure & social', '#db7c65'),
    'travel': ('Travel', '#5385e8'),
    'other': ('Learning & other', '#9ca3af'),
}
ALIASES = {
    'subscription': 'home', 'subscriptions': 'home',
    'personal_care': 'shopping', 'personal-care': 'shopping', 'personalcare': 'shopping',
    'dining': 'food', 'groceries': 'food', 'medical': 'health',
    'gym': 'health', 'fitness': 'health', 'insurance': 'health',
    'entertainment': 'social', 'dates': 'social', 'gifts': 'social',
    'education': 'other', 'learning': 'other',
    **{label.lower(): key for key, (label, _) in CATEGORIES.items()},
}


def migrated_category(value, note=''):
    value = value.lower()
    if value == 'subscription':
        name = note.lower()
        if any(term in name for term in ('gym', 'fitness', 'insurance', 'medical')):
            return 'health'
        if any(term in name for term in ('netflix', 'spotify', 'disney', 'youtube', 'apple music', 'prime video')):
            return 'social'
    return ALIASES.get(value, value)


def migrate_categories(db):
    if db.execute("SELECT 1 FROM meta WHERE key='eight_categories_v1'").fetchone():
        return
    for table in ('expenses', 'subscriptions'):
        description = 'note' if table == 'expenses' else 'name'
        for row in db.execute(f'SELECT id,category,{description} description FROM {table}').fetchall():
            value = migrated_category(row['category'], row['description'])
            if value != row['category']:
                db.execute(f'UPDATE {table} SET category=? WHERE id=?', (value, row['id']))

    # Merge limits using each old category's carried-forward value at every change.
    rows = db.execute('SELECT * FROM category_budgets ORDER BY month,category').fetchall()
    current, merged = {}, []
    for month, changes in groupby(rows, key=lambda row: row['month']):
        touched = set()
        for row in changes:
            current[row['category']] = row['amount']
            touched.add(migrated_category(row['category']))
        for cat in sorted(touched):
            limits = [amount for old, amount in current.items()
                      if migrated_category(old) == cat and amount is not None]
            merged.append((month, cat, sum(limits) if limits else None))
    db.execute('DELETE FROM category_budgets')
    db.executemany('INSERT INTO category_budgets(month,category,amount) VALUES (?,?,?)', merged)

    # Normalize saved previews as well as their optimistic-lock snapshots.
    def normalize(value):
        if isinstance(value, dict):
            result = {key: normalize(item) for key, item in value.items()}
            if isinstance(result.get('category'), str):
                result['category'] = migrated_category(result['category'], result.get('note') or '')
            return result
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    for row in db.execute('SELECT id,kind,payload FROM drafts').fetchall():
        payload = json.loads(row['payload'])
        updated = normalize(payload)
        if updated != payload:
            if row['kind'] == 'csv':
                # Its duplicate fingerprints refer to the earlier categories.
                db.execute("UPDATE drafts SET status='cancelled' WHERE id=? AND status='pending'", (row['id'],))
                continue
            db.execute('UPDATE drafts SET payload=?,version=version+1 WHERE id=?', (json.dumps(updated), row['id']))
    db.execute("INSERT INTO meta VALUES ('eight_categories_v1','done')")
