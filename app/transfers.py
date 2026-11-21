"""CSV transfers with a reviewed, atomic import and spreadsheet-safe text export."""
import csv
import hashlib
import io
import json
import time
from collections import Counter

from .features import currency_fields

MAX_CSV = 2 * 1024 * 1024
FIELDS = ['spent_on','amount','category','note','original_currency','original_amount']


def safe_text(value):
    value = str(value or '')
    return "'" + value if value.startswith("'") or value.lstrip().startswith(('=','+','-','@','\t','\r')) else value


def fingerprint(values):
    return hashlib.sha256(json.dumps(list(values), ensure_ascii=False, separators=(',',':')).encode()).hexdigest()


def row_signature(row):
    return fingerprint([row['amount'],row['category'],row['note'],row['spent_on'],row.get('original_currency'),row.get('original_amount')])


def export_csv(ledger, month=None):
    from .core import month_bounds
    ledger.sync_subscriptions()
    with ledger.db() as db:
        if month:
            first,last=month_bounds(month)
            rows = db.execute('SELECT * FROM expenses WHERE deleted=0 AND spent_on BETWEEN ? AND ? ORDER BY spent_on,id', (first.isoformat(),last.isoformat())).fetchall()
        else:
            rows = db.execute('SELECT * FROM expenses WHERE deleted=0 ORDER BY spent_on,id').fetchall()
    out=io.StringIO(newline='')
    writer=csv.writer(out)
    writer.writerow(FIELDS)
    for row in rows:
        writer.writerow([row['spent_on'], f"{row['amount']/100:.2f}", row['category'],safe_text(row['note']),row['original_currency'] or '',row['original_amount'] or ''])
    return '\ufeff'+out.getvalue()


def preview_csv(ledger, content):
    if not content or len(content)>MAX_CSV:
        raise ValueError('Upload a non-empty CSV file of at most 2 MB.')
    try:
        text=content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Save the CSV as UTF-8 before importing.') from None
    reader=csv.DictReader(io.StringIO(text,newline=''), strict=True)
    headers=reader.fieldnames
    if not headers or len(set(headers))!=len(headers):
        raise ValueError('CSV must have a header row without duplicate column names.')
    if not {'spent_on','amount','category'} <= set(headers):
        raise ValueError('Required CSV columns: spent_on, amount, category. Optional: note, original_currency, original_amount.')
    rows=[]
    seen=Counter()
    with ledger.db() as db:
        existing=Counter(row_signature(dict(row)) for row in db.execute('SELECT * FROM expenses'))
    try:
        for line,raw in enumerate(reader,start=2):
            if len(rows)>=5000:
                raise ValueError('Import at most 5,000 rows per file.')
            if None in raw or any(raw.get(key) is None for key in ('spent_on','amount','category')):
                raise ValueError(f'Row {line}: wrong number of columns.')
            note=raw.get('note','') or ''
            if note.startswith("''") or note.startswith("'") and note[1:].lstrip().startswith(('=','+','-','@','\t','\r')):
                note=note[1:]
            payload={key:raw.get(key,'').strip() for key in FIELDS if key!='note'}
            payload['note']=note
            try:
                values=ledger.validate_expense(payload)
                original=currency_fields(payload)
            except ValueError as error:
                raise ValueError(f'Row {line}: {error}') from None
            signature=fingerprint([*values,*original])
            seen[signature]+=1
            rows.append({'payload':payload,'signature':signature,'occurrence':seen[signature],
                         'duplicate':seen[signature]<=existing[signature], 'line':line})
    except csv.Error:
        raise ValueError('The CSV could not be read. Check quotes and column separators.') from None
    if not rows:
        raise ValueError('CSV has no expense rows.')
    draft=ledger.new_draft('csv', {'rows':rows})
    return {'id':draft,'rows':rows,'total_rows':len(rows),'new_rows':sum(not row['duplicate'] for row in rows)}


def confirm_csv(ledger, identifier):
    with ledger.db() as db:
        db.execute('BEGIN IMMEDIATE')
        draft=db.execute("SELECT * FROM drafts WHERE id=? AND kind='csv' AND expires>?",(identifier,int(time.time()))).fetchone()
        if not draft:
            raise LookupError('Import preview expired. Upload the CSV again.')
        if draft['status']=='cancelled':
            raise ValueError('Import was cancelled.')
        if draft['status']=='saved':
            return {'imported':0,'skipped':len(json.loads(draft['payload'])['rows']),'already_confirmed':True}
        rows=json.loads(draft['payload'])['rows']
        existing=Counter(row_signature(dict(row)) for row in db.execute('SELECT * FROM expenses'))
        imported=0
        for row in rows:
            if row['occurrence']<=existing[row['signature']]:
                continue
            values=ledger.validate_expense(row['payload'])
            original=currency_fields(row['payload'])
            key=f"csv:{row['signature']}:{row['occurrence']}"
            result=db.execute('''INSERT OR IGNORE INTO expenses(amount,category,note,spent_on,original_currency,original_amount,source,source_key)
                         VALUES (?,?,?,?,?,?,'csv',?)''',(*values,*original,key))
            imported+=result.rowcount
        db.execute("UPDATE drafts SET status='saved' WHERE id=?",(identifier,))
    return {'imported':imported,'skipped':len(rows)-imported,'already_confirmed':False}
