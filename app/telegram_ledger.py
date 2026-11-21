"""Durable, versioned Telegram proposals. Ledger mutations require explicit confirmation."""
import json
import secrets
import time

from .features import currency_fields

EXPENSE_FIELDS=('amount','category','note','spent_on','original_currency','original_amount')


class TelegramLedgerMixin:
    def initialize_telegram(self, db):
        columns={row['name'] for row in db.execute('PRAGMA table_info(drafts)')}
        if 'version' not in columns:
            db.execute('ALTER TABLE drafts ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        db.execute('''CREATE TABLE IF NOT EXISTS telegram_edit_prompts(
            chat_id INTEGER NOT NULL,thread_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
            draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,version INTEGER NOT NULL,
            consumed_update INTEGER,PRIMARY KEY(chat_id,thread_id,message_id))''')

    def expense(self, identifier):
        with self.db() as db:
            row=db.execute('SELECT * FROM expenses WHERE id=? AND deleted=0',(identifier,)).fetchone()
        if not row:
            raise LookupError('Expense not found or already deleted. Use /recent to find an entry.')
        return dict(row)

    def recent_expenses(self, month=None, page=0):
        from .core import month_bounds, today
        if type(page) is not int or not 0<=page<=100000:
            raise ValueError('Invalid expense page.')
        self.sync_subscriptions()
        where='deleted=0';args=[]
        if month:
            first,last=month_bounds(month)
            if first>today():raise ValueError('Select the current month or an earlier month.')
            where+=' AND spent_on BETWEEN ? AND ?';args=[first.isoformat(),last.isoformat()]
        with self.db() as db:
            total=db.execute('SELECT COUNT(*) total FROM expenses WHERE '+where,args).fetchone()['total']
            page=min(page,max(0,(total-1)//10))
            rows=[dict(row) for row in db.execute('SELECT * FROM expenses WHERE '+where+' ORDER BY spent_on DESC,id DESC LIMIT 10 OFFSET ?',[*args,page*10])]
        return {'expenses':rows,'total':total,'page':page,'pages':max(1,(total+9)//10)}

    def check_telegram_context(self, draft, user_id, chat_id, thread_id):
        payload=draft['payload']
        actual=(user_id,chat_id,thread_id)
        expected=(payload.get('_telegram_user_id',user_id),payload.get('_telegram_chat_id',user_id),payload.get('_telegram_thread_id',0))
        if actual!=expected:
            raise ValueError('Open this draft in its original chat and topic.')

    def propose_expense(self, operation, payload, user_id, chat_id, thread_id, update_id, identifier=None):
        if operation not in ('add','edit','delete'):raise ValueError('Unknown expense action.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM drafts WHERE expires<?',(int(time.time()),))
            source=f'telegram-proposal:{user_id}:{update_id}'
            old=db.execute('SELECT id FROM drafts WHERE source_key=?',(source,)).fetchone()
            if old:return old['id']
            before=None
            if operation!='add':
                before=db.execute('SELECT * FROM expenses WHERE id=? AND deleted=0',(identifier,)).fetchone()
                if not before:raise LookupError('Expense not found or already deleted. Use /recent to find an entry.')
                before=dict(before)
            values=dict(payload or {})
            if operation=='delete':
                values={key:before.get(key) for key in EXPENSE_FIELDS}
                values['amount']=f"{before['amount']/100:.2f}"
            else:
                self.validate_expense(values);currency_fields(values)
            values.update(_operation=operation,_expense_id=identifier,_before=before,
                          _telegram_user_id=user_id,_telegram_chat_id=chat_id,_telegram_thread_id=thread_id,
                          _expense_source_key=f'telegram:{user_id}:{update_id}')
            proposal=secrets.token_hex(12)
            # A retried update written by the old immediate-save bot must not be logged twice after upgrading.
            existing=db.execute('SELECT * FROM expenses WHERE source_key=?',(values['_expense_source_key'],)).fetchone() if operation=='add' else None
            if existing:values['_result_id']=existing['id']
            db.execute('INSERT INTO drafts(id,kind,payload,expires,status,source_key) VALUES (?,\'telegram_expense\',?,?,?,?)',
                       (proposal,json.dumps(values),int(time.time())+7*86400,'saved' if existing else 'pending',source))
        return proposal

    def confirm_expense_proposal(self, identifier, user_id, chat_id, thread_id, version):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM drafts WHERE id=? AND kind='telegram_expense' AND expires>?",(identifier,int(time.time()))).fetchone()
            if not row:raise LookupError('Draft not found or expired. Start again with /add, /edit or /delete.')
            draft=dict(row);draft['payload']=json.loads(row['payload']);payload=draft['payload']
            self.check_telegram_context(draft,user_id,chat_id,thread_id)
            if draft['version']!=version:raise ValueError('This preview is outdated. Use the latest confirmation buttons.')
            if draft['status']=='cancelled':raise ValueError('This draft was cancelled.')
            if draft['status']=='saved':
                result=db.execute('SELECT * FROM expenses WHERE id=?',(payload['_result_id'],)).fetchone()
                return {'expense':dict(result),'operation':payload['_operation'],'already_confirmed':True}
            operation=payload['_operation']
            if operation=='add':
                values=self.validate_expense(payload);original=currency_fields(payload)
                db.execute('''INSERT OR IGNORE INTO expenses(amount,category,note,spent_on,original_currency,original_amount,source,source_key)
                            VALUES (?,?,?,?,?,?,'telegram',?)''',(*values,*original,payload['_expense_source_key']))
                result=db.execute('SELECT * FROM expenses WHERE source_key=?',(payload['_expense_source_key'],)).fetchone()
            else:
                current=db.execute('SELECT * FROM expenses WHERE id=? AND deleted=0',(payload['_expense_id'],)).fetchone()
                if not current:raise LookupError('Expense was deleted after this preview. Use /recent to refresh.')
                if any(current[key]!=payload['_before'].get(key) for key in EXPENSE_FIELDS):
                    raise ValueError('Expense changed after this preview. Start /edit or /delete again to review its latest values.')
                if operation=='edit':
                    values=self.validate_expense(payload);original=currency_fields(payload)
                    db.execute('UPDATE expenses SET amount=?,category=?,note=?,spent_on=?,original_currency=?,original_amount=? WHERE id=?',(*values,*original,current['id']))
                else:
                    db.execute('UPDATE expenses SET deleted=1 WHERE id=?',(current['id'],))
                result=db.execute('SELECT * FROM expenses WHERE id=?',(current['id'],)).fetchone()
            payload['_result_id']=result['id']
            db.execute("UPDATE drafts SET payload=?,status='saved' WHERE id=?",(json.dumps(payload),identifier))
            return {'expense':dict(result),'operation':operation,'already_confirmed':False}

    def revise_telegram_draft(self, identifier, payload, user_id, chat_id, thread_id, version, prompt=None, update_id=None):
        self.validate_expense(payload);currency_fields(payload)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM drafts WHERE id=? AND kind IN ('telegram_expense','receipt') AND expires>?",(identifier,int(time.time()))).fetchone()
            if not row:raise LookupError('Draft not found or expired.')
            draft=dict(row);draft['payload']=json.loads(row['payload'])
            self.check_telegram_context(draft,user_id,chat_id,thread_id)
            if prompt is not None:
                entry=db.execute('SELECT * FROM telegram_edit_prompts WHERE chat_id=? AND thread_id=? AND message_id=?',(chat_id,thread_id,prompt)).fetchone()
                if not entry or entry['draft_id']!=identifier:raise ValueError('Edit prompt not found. Tap Edit again.')
                if entry['consumed_update'] is not None:
                    if entry['consumed_update']==update_id:return
                    raise ValueError('That edit prompt was already used. Tap the latest Edit button.')
            if draft['status']!='pending':raise ValueError('This draft has already been confirmed or cancelled.')
            if draft['version']!=version:raise ValueError('This edit prompt is outdated. Tap the latest Edit button.')
            if draft['payload'].get('_operation')=='delete':raise ValueError('Cancel the deletion before editing this expense.')
            revised={**draft['payload'],**{key:payload.get(key) for key in EXPENSE_FIELDS}}
            db.execute('UPDATE drafts SET payload=?,version=version+1 WHERE id=?',(json.dumps(revised),identifier))
            if prompt is not None:
                db.execute('UPDATE telegram_edit_prompts SET consumed_update=? WHERE chat_id=? AND thread_id=? AND message_id=?',(update_id,chat_id,thread_id,prompt))

    def cancel_telegram_draft(self, identifier, user_id, chat_id, thread_id, version):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM drafts WHERE id=? AND kind IN ('telegram_expense','receipt') AND expires>?",(identifier,int(time.time()))).fetchone()
            if not row:raise LookupError('Draft not found or expired.')
            draft=dict(row);draft['payload']=json.loads(row['payload'])
            self.check_telegram_context(draft,user_id,chat_id,thread_id)
            if draft['version']!=version:raise ValueError('This preview is outdated. Use the latest buttons.')
            if draft['status']=='saved':raise ValueError('Already confirmed. Use /edit or /delete for the saved expense.')
            db.execute("UPDATE drafts SET status='cancelled' WHERE id=?",(identifier,))

    def register_telegram_prompt(self, identifier, version, user_id, chat_id, thread_id, message_id):
        draft=self.draft(identifier)
        self.check_telegram_context(draft,user_id,chat_id,thread_id)
        if draft['status']!='pending' or draft['version']!=version:raise ValueError('This draft changed. Tap the latest Edit button.')
        with self.db() as db:
            db.execute('INSERT INTO telegram_edit_prompts(chat_id,thread_id,message_id,draft_id,version) VALUES (?,?,?,?,?)',
                       (chat_id,thread_id,message_id,identifier,version))

    def telegram_prompt(self, chat_id, thread_id, message_id):
        with self.db() as db:
            row=db.execute('''SELECT p.* FROM telegram_edit_prompts p JOIN drafts d ON d.id=p.draft_id
                              WHERE p.chat_id=? AND p.thread_id=? AND p.message_id=? AND d.expires>?''',
                           (chat_id,thread_id,message_id,int(time.time()))).fetchone()
        return dict(row) if row else None
