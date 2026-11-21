"""Known recurring contributions, installed once without changing later user edits."""


def initialize_contributions(db, as_of):
    db.execute('''CREATE TABLE IF NOT EXISTS investment_contributions (
        id INTEGER PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount>0),
        note TEXT NOT NULL, invested_on TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    if db.execute("SELECT 1 FROM meta WHERE key='recurring_contributions_v1'").fetchone():
        return
    month = as_of.strftime('%Y-%m')
    plans = [('Household allowance contribution', 40120, 25, 'home', 'expense'),
             ('Recurring investments', 60000, 4, 'other', 'investment')]
    for name, amount, day, cat, kind in plans:
        if not db.execute('SELECT 1 FROM subscriptions WHERE lower(name)=lower(?)', (name,)).fetchone():
            db.execute('''INSERT INTO subscriptions(name,amount,billing_day,start_month,start_date,category,payment_type)
                VALUES (?,?,?,?,?,?,?)''', (name, amount, day, month, f'{month}-{day:02d}', cat, kind))
    db.execute("INSERT INTO meta VALUES ('recurring_contributions_v1','done')")
