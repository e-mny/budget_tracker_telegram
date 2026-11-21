"""Telegram previews, record actions, and reply-based editing."""
import re
from datetime import date

from .core import CATEGORIES, money, parse_add
from .telegram_ledger import EXPENSE_FIELDS


def expense_payload(row):
    return {**{key:row.get(key) for key in EXPENSE_FIELDS},'amount':f"{row['amount']/100:.2f}"}


def expense_identifier(text):
    if not re.fullmatch(r'[0-9]{1,19}',text) or not 1<=int(text)<=9223372036854775807:
        raise ValueError('Use a valid expense ID from /recent.')
    return int(text)


def parse_revision(text, previous):
    clear=re.search(r'\s+--original\s+none\s*$',text,re.I)
    if clear:text=text[:clear.start()]
    result=parse_add(text,as_of=date.fromisoformat(previous['spent_on']))
    if 'original_currency' not in result:
        result['original_currency']=None if clear else previous.get('original_currency')
        result['original_amount']=None if clear else previous.get('original_amount')
    return result


class TelegramEditorMixin:
    def context(self,message):
        return self.user_id,message['chat']['id'],message.get('message_thread_id',0)

    def send_keyboard(self,text,keyboard,**destination):
        payload={'chat_id':destination.get('chat_id',self.user_id),'text':text,
                 'reply_markup':{'inline_keyboard':keyboard}}
        if destination.get('thread_id'):payload['message_thread_id']=destination['thread_id']
        return self.telegram('sendMessage',**payload)

    def entry_text(self,payload):
        lines=[f"SGD amount: {money(self.ledger.validate_expense(payload)[0])}",
               f"Category: {CATEGORIES[payload['category']][0]}",f"Date: {payload['spent_on']}",
               f"Description: {payload.get('note') or '(none)'}"]
        if payload.get('original_currency'):
            lines.append(f"Original: {payload['original_currency']} {payload.get('original_amount') or '(missing)'}")
        return '\n'.join(lines)

    def record_buttons(self,identifier):
        return [[{'text':f'Edit #{identifier}','callback_data':f'expense-edit:{identifier}'},
                 {'text':f'Delete #{identifier}','callback_data':f'expense-delete:{identifier}'}]]

    def saved_result(self,row,operation='add',already=False,**destination):
        if row['deleted']:
            text=f"Expense #{row['id']} is deleted." if already else f"Deleted expense #{row['id']} · {money(row['amount'])}."
            return self.send(text,**destination)
        label='Already confirmed' if already else 'Updated' if operation=='edit' else 'Saved'
        summary=self.ledger.summary(row['spent_on'][:7])
        text=f"{label} #{row['id']}\n{self.entry_text(expense_payload(row))}\nMonth total: {money(summary['total'])}"
        return self.send_keyboard(text,self.record_buttons(row['id']),**destination)

    def proposal_preview(self,identifier,**destination):
        draft=self.ledger.draft(identifier,'telegram_expense');payload=draft['payload']
        if draft['status']=='saved':
            with self.ledger.db() as db:
                row=dict(db.execute('SELECT * FROM expenses WHERE id=?',(payload['_result_id'],)).fetchone())
            return self.saved_result(row,payload['_operation'],already=True,**destination)
        if draft['status']=='cancelled':return self.send('This draft was cancelled. Nothing changed.',**destination)
        operation=payload['_operation'];version=draft['version']
        title={'add':'Review new expense','edit':f"Review changes to expense #{payload['_expense_id']}",
               'delete':f"Delete expense #{payload['_expense_id']}?"}[operation]
        text=title+'\n'+self.entry_text(payload)
        if operation=='edit':text+='\n\nCurrently recorded:\n'+self.entry_text(expense_payload(payload['_before']))
        text+=f'\n\nDraft ID: {identifier}\nNothing changes until you confirm.'
        if operation=='delete' and payload['_before']['source']=='subscription':
            text+=' This month’s charge will not be recreated; future subscription charges continue.'
        keyboard=[{'text':'Confirm delete' if operation=='delete' else 'Confirm changes' if operation=='edit' else 'Confirm expense',
                   'callback_data':f'entry-confirm:{identifier}:{version}'}]
        if operation!='delete':keyboard.append({'text':'Edit','callback_data':f'entry-edit:{identifier}:{version}'})
        keyboard.append({'text':'Cancel','callback_data':f'entry-cancel:{identifier}:{version}'})
        return self.send_keyboard(text,[keyboard],**destination)

    def recent_preview(self,message,month=None,page=0):
        result=self.ledger.recent_expenses(month,page)
        heading=f"Expenses · {month or 'all dates'} · page {result['page']+1}/{result['pages']}"
        lines=[heading];keyboard=[]
        for row in result['expenses']:
            lines.append(f"#{row['id']} · {row['spent_on']} · {money(row['amount'])} · {CATEGORIES[row['category']][0]}\n{(row['note'] or 'No description')[:100]}")
            keyboard.extend(self.record_buttons(row['id']))
        if not result['expenses']:lines.append('No recorded expenses in this view.')
        navigation=[];scope=month or 'all'
        if result['page']>0:navigation.append({'text':'Newer','callback_data':f'expense-page:{scope}:{result["page"]-1}'})
        if result['page']+1<result['pages']:navigation.append({'text':'Older','callback_data':f'expense-page:{scope}:{result["page"]+1}'})
        if navigation:keyboard.append(navigation)
        return self.send_keyboard('\n\n'.join(lines),keyboard,**self.destination(message))

    def editor_prompt(self,identifier,message,version=None):
        draft=self.ledger.draft(identifier)
        self.ledger.check_telegram_context(draft,*self.context(message))
        if draft['kind'] not in ('telegram_expense','receipt') or draft['status']!='pending':
            raise ValueError('This draft is already confirmed or cancelled. Use /edit for a recorded expense.')
        if version is not None and draft['version']!=version:raise ValueError('This preview is outdated. Tap the latest Edit button.')
        payload=draft['payload']
        if payload.get('_operation')=='delete':raise ValueError('Cancel the deletion before editing the expense.')
        current=f"{payload.get('amount') or '0.00'} {payload.get('category','other')} {payload.get('note','')} @{payload['spent_on']}"
        if payload.get('original_currency'):current+=f" --original {payload['original_currency']} {payload.get('original_amount') or '0'}"
        text=f'You can edit this entry by replying with:\namount category description @YYYY-MM-DD\n\nCurrent entry (copy and change):\n{current}\n\nThe date and original currency are kept if omitted. Use --original none to remove the original currency. Your reply creates a new preview; confirm it to save.'
        route=self.destination(message)
        response=self.telegram('sendMessage',chat_id=route.get('chat_id',self.user_id),text=text,
            **({'message_thread_id':route['thread_id']} if route.get('thread_id') else {}),
            entities=[{'type':'text_mention','offset':0,'length':3,'user':{'id':self.user_id,'is_bot':False,'first_name':'Owner'}}],
            reply_markup={'force_reply':True,'selective':True,'input_field_placeholder':'7.50 food Corrected description'})
        if not isinstance(response,dict) or not response.get('message_id'):
            raise ValueError(f'Use /edit {identifier} followed by the corrected entry to continue.')
        self.ledger.register_telegram_prompt(identifier,draft['version'],*self.context(message),response['message_id'])

    def handle_editor_reply(self,update_id,message):
        replied=message.get('reply_to_message',{}).get('message_id')
        if not replied or not message.get('text'):return False
        prompt=self.ledger.telegram_prompt(message['chat']['id'],message.get('message_thread_id',0),replied)
        if not prompt:return False
        try:
            draft=self.ledger.draft(prompt['draft_id'])
            self.ledger.check_telegram_context(draft,*self.context(message))
            payload=parse_revision(message['text'],draft['payload'])
            self.ledger.revise_telegram_draft(draft['id'],payload,*self.context(message),prompt['version'],prompt=replied,update_id=update_id)
            self.preview_any_draft(draft['id'],message)
        except (ValueError,LookupError) as error:
            self.send(str(error),**self.destination(message))
        return True

    def preview_any_draft(self,identifier,message):
        draft=self.ledger.draft(identifier)
        self.ledger.check_telegram_context(draft,*self.context(message))
        if draft['kind']=='receipt':return self.receipt_preview(identifier,**self.destination(message))
        if draft['kind']=='telegram_expense':return self.proposal_preview(identifier,**self.destination(message))
        raise ValueError('This draft cannot be edited through Telegram.')

    def edit_command(self,args,message,request_key):
        target,_,text=args.strip().partition(' ')
        if not target:return self.recent_preview(message)
        if target.isdigit() and len(target)<=19:
            old=self.ledger.expense(expense_identifier(target));base=expense_payload(old)
            payload=parse_revision(text,base) if text.strip() else base
            identifier=self.ledger.propose_expense('edit',payload,*self.context(message),request_key,identifier=old['id'])
        elif re.fullmatch(r'[0-9a-f]{24}',target):
            identifier=target;draft=self.ledger.draft(identifier)
            if draft['kind'] not in ('telegram_expense','receipt'):raise ValueError('This draft cannot be edited through Telegram.')
            self.ledger.check_telegram_context(draft,*self.context(message))
            if text.strip():
                payload=parse_revision(text,draft['payload'])
                self.ledger.revise_telegram_draft(identifier,payload,*self.context(message),draft['version'])
        else:raise ValueError('Use /edit EXPENSE_ID, or /edit EXPENSE_ID 7.50 food Corrected description.')
        if text.strip():return self.preview_any_draft(identifier,message)
        return self.editor_prompt(identifier,message)

    def delete_command(self,args,message,request_key):
        target=args.strip()
        if not target:return self.recent_preview(message)
        if not target.isdigit():raise ValueError('Use /delete EXPENSE_ID. Use /recent to find the ID.')
        identifier=self.ledger.propose_expense('delete',None,*self.context(message),request_key,identifier=expense_identifier(target))
        return self.proposal_preview(identifier,**self.destination(message))
