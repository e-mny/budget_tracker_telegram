"""Owner-confirmed expense CRUD and edit prompts across private/group chats."""
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from app import core
from app.bot import Bot,TelegramError
from app.core import Ledger
from app.receipts import parse_receipt

OWNER=123
GROUP=-1001234567890


@pytest.fixture
def ledger(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'today',lambda:date(2026,11,21))
    ledger=Ledger(tmp_path/'ledger.sqlite3');ledger.sync_subscriptions()
    return ledger


@pytest.fixture
def bot(ledger,monkeypatch):
    bot=Bot(ledger,'test-token',OWNER,group_id=GROUP)
    bot.username='PennyBot';bot.bot_id=456;bot.calls=[]
    def telegram(method,files=None,**payload):
        bot.calls.append((method,payload))
        return {'message_id':len(bot.calls),'from':{'id':456,'is_bot':True}}
    monkeypatch.setattr(bot,'telegram',telegram)
    return bot


def command(bot,text,update=1,chat=OWNER,topic=0,reply_to=None,user=OWNER):
    message={'from':{'id':user},'chat':{'id':chat,'type':'private' if chat==OWNER else 'supergroup'},'text':text}
    if topic:message['message_thread_id']=topic
    if reply_to:message['reply_to_message']={'message_id':reply_to,'from':{'id':456,'is_bot':True}}
    bot.process_update({'update_id':update,'message':message})


def button(bot,label):
    for method,payload in reversed(bot.calls):
        if method=='sendMessage':
            for row in payload.get('reply_markup',{}).get('inline_keyboard',[]):
                for item in row:
                    if item['text']==label:return item['callback_data']
    raise AssertionError(f'Missing {label} button')


def click(bot,data,query='q1',chat=OWNER,topic=0,user=OWNER):
    message={'chat':{'id':chat,'type':'private' if chat==OWNER else 'supergroup'}}
    if topic:message['message_thread_id']=topic
    bot.process_update({'callback_query':{'id':query,'from':{'id':user},'message':message,'data':data}})


def last_text(bot):
    return next(payload['text'] for method,payload in reversed(bot.calls) if method=='sendMessage')


@pytest.mark.parametrize('chat,topic',[(OWNER,0),(GROUP,7)])
def test_add_only_saved_after_owner_confirmation(bot,ledger,chat,topic):
    command(bot,'/add@PennyBot 6.50 food Lunch',chat=chat,topic=topic)
    assert ledger.summary()['total']==12410
    assert 'Review new expense' in last_text(bot)
    confirm=button(bot,'Confirm expense')
    click(bot,confirm,chat=chat,topic=topic,user=999)
    click(bot,confirm,chat=GROUP if chat==OWNER else OWNER,topic=topic)
    assert ledger.summary()['total']==12410
    click(bot,confirm,chat=chat,topic=topic)
    assert ledger.summary()['total']==13060
    click(bot,confirm,chat=chat,topic=topic)
    assert ledger.summary()['total']==13060 and 'Already confirmed' in last_text(bot)
    assert bot.calls[-1][1]['chat_id']==chat
    if topic:assert bot.calls[-1][1]['message_thread_id']==topic


def test_cancelled_add_cannot_be_saved(bot,ledger):
    command(bot,'/add 8 food Lunch')
    confirm=button(bot,'Confirm expense');cancel=button(bot,'Cancel')
    click(bot,cancel);click(bot,confirm)
    assert ledger.summary()['total']==12410
    assert 'cancelled' in last_text(bot)


def test_edit_pending_entry_by_reply_requires_new_confirmation(bot,ledger):
    command(bot,'/add 6.50 food Lunch')
    original_confirm=button(bot,'Confirm expense')
    click(bot,button(bot,'Edit'))
    prompt=len(bot.calls)
    assert bot.calls[-1][1]['reply_markup']['force_reply']
    command(bot,'7.50 food Better lunch',update=2,reply_to=prompt)
    assert ledger.summary()['total']==12410 and 'S$7.50' in last_text(bot)
    current_confirm=button(bot,'Confirm expense')
    click(bot,original_confirm)
    assert 'outdated' in last_text(bot) and ledger.summary()['total']==12410
    click(bot,current_confirm)
    assert ledger.summary()['total']==13160


def test_invalid_reply_can_be_corrected_and_prompt_replay_is_idempotent(bot,ledger):
    command(bot,'/add 6 food Lunch');click(bot,button(bot,'Edit'))
    prompt=len(bot.calls)
    command(bot,'not an amount',update=2,reply_to=prompt)
    assert 'amount' in last_text(bot).lower() or 'Unknown category' in last_text(bot)
    command(bot,'9 food Lunch',update=3,reply_to=prompt)
    confirm=button(bot,'Confirm expense')
    command(bot,'9 food Lunch',update=3,reply_to=prompt)
    assert button(bot,'Confirm expense')==confirm
    command(bot,'10 food Lunch',update=4,reply_to=prompt)
    assert 'already used' in last_text(bot)
    click(bot,confirm)
    assert ledger.summary()['total']==13310


def test_recorded_edit_preserves_date_currency_and_requires_confirmation(bot,ledger):
    row=ledger.add_expense({'amount':'25','category':'travel','note':'Original','spent_on':'2026-10-10','original_currency':'USD','original_amount':'18.50'})
    command(bot,f'/edit {row["id"]} 26 travel Corrected')
    confirm=button(bot,'Confirm changes')
    assert ledger.expense(row['id'])['amount']==2500
    assert 'Currently recorded' in last_text(bot)
    click(bot,confirm)
    edited=ledger.expense(row['id'])
    assert edited['amount']==2600 and edited['spent_on']=='2026-10-10'
    assert edited['original_currency']=='USD' and edited['original_amount']=='18.50'
    assert ledger.summary('2026-10')['total']==2600
    command(bot,f'/edit {row["id"]} 27 travel Corrected --original none',update=2)
    click(bot,button(bot,'Confirm changes'),query='q2')
    assert ledger.expense(row['id'])['original_currency'] is None


def test_edit_saved_expense_button_uses_reply_prompt(bot,ledger):
    row=ledger.add_expense({'amount':'5','category':'food','note':'Lunch'})
    command(bot,'/recent')
    click(bot,button(bot,f'Edit #{row["id"]}'))
    prompt=len(bot.calls)
    command(bot,'7 food Changed lunch',update=2,reply_to=prompt)
    assert ledger.expense(row['id'])['amount']==500
    click(bot,button(bot,'Confirm changes'),query='q2')
    assert ledger.expense(row['id'])['amount']==700


def test_edit_preview_does_not_overwrite_concurrent_dashboard_change(bot,ledger):
    row=ledger.add_expense({'amount':'5','category':'food'})
    command(bot,f'/edit {row["id"]} 7 food Changed')
    confirm=button(bot,'Confirm changes')
    ledger.update_expense(row['id'],{'amount':'8','category':'food','note':'Dashboard correction'})
    click(bot,confirm)
    assert 'changed after this preview' in last_text(bot)
    assert ledger.expense(row['id'])['amount']==800


def test_delete_has_review_cancel_confirm_and_recurring_tombstone(bot,ledger):
    row=ledger.summary()['expenses'][0]
    command(bot,f'/delete {row["id"]}')
    confirm=button(bot,'Confirm delete')
    assert ledger.summary()['count']==3 and 'future subscription charges continue' in last_text(bot)
    click(bot,button(bot,'Cancel'));click(bot,confirm)
    assert ledger.summary()['count']==3
    command(bot,f'/delete {row["id"]}',update=2)
    confirm=button(bot,'Confirm delete')
    click(bot,confirm,query='q2');click(bot,confirm,query='q2')
    assert ledger.summary()['count']==2
    ledger.sync_subscriptions()
    assert ledger.summary()['count']==2


def test_stale_delete_does_not_remove_newer_values(bot,ledger):
    row=ledger.add_expense({'amount':'5','category':'food'})
    command(bot,f'/delete {row["id"]}')
    confirm=button(bot,'Confirm delete')
    ledger.update_expense(row['id'],{'amount':'6','category':'food'})
    click(bot,confirm)
    assert 'changed after this preview' in last_text(bot)
    assert ledger.expense(row['id'])['amount']==600


def test_pending_drafts_prompts_and_confirmation_survive_restart(bot,ledger,monkeypatch):
    command(bot,'/add 5 food Lunch');click(bot,button(bot,'Edit'))
    prompt=len(bot.calls)
    bot.ledger=Ledger(ledger.path)
    command(bot,'6 food Corrected',update=2,reply_to=prompt)
    confirm=button(bot,'Confirm expense')
    bot.ledger=Ledger(ledger.path)
    click(bot,confirm)
    assert ledger.summary()['total']==13010


def test_wrong_topic_reply_does_not_edit_group_draft(bot,ledger):
    command(bot,'/add@PennyBot 5 food Lunch',chat=GROUP,topic=7)
    click(bot,button(bot,'Edit'),chat=GROUP,topic=7)
    prompt=len(bot.calls)
    command(bot,'99 food Wrong topic',update=2,chat=GROUP,topic=8,reply_to=prompt)
    assert ledger.summary()['total']==12410
    command(bot,'6 food Corrected',update=3,chat=GROUP,topic=7,reply_to=prompt)
    click(bot,button(bot,'Confirm expense'),chat=GROUP,topic=7)
    assert ledger.summary()['total']==13010


def test_recent_pagination_and_month_filter(bot,ledger):
    for index in range(15):ledger.add_expense({'amount':'1','category':'food','note':f'Meal {index}'})
    command(bot,'/recent')
    click(bot,button(bot,'Older'))
    assert 'page 2/2' in last_text(bot)
    click(bot,button(bot,'Newer'))
    assert 'page 1/2' in last_text(bot)
    command(bot,'/recent 2026-10',update=2)
    assert 'No recorded expenses' in last_text(bot)
    command(bot,'/edit',update=3)
    assert 'Expenses' in last_text(bot)
    command(bot,'/delete',update=4)
    assert 'Expenses' in last_text(bot)


def test_receipt_buttons_are_versioned_and_editable(bot,ledger):
    identifier=ledger.new_draft('receipt',parse_receipt('CAFE\nTOTAL SGD 5.00'))
    bot.receipt_preview(identifier)
    old=button(bot,'Save receipt')
    click(bot,button(bot,'Edit'))
    prompt=len(bot.calls)
    command(bot,'6 food Changed receipt',update=2,reply_to=prompt)
    new=button(bot,'Save receipt')
    click(bot,old)
    assert 'outdated' in last_text(bot) and ledger.summary()['total']==12410
    click(bot,new)
    assert ledger.summary()['total']==13010
    assert 'expense-edit:' in button(bot,'Edit #4')


def test_expired_draft_and_malformed_buttons_do_not_mutate(bot,ledger):
    command(bot,'/add 5 food Lunch');confirm=button(bot,'Confirm expense')
    identifier=confirm.split(':')[1]
    with ledger.db() as db:db.execute('UPDATE drafts SET expires=? WHERE id=?',(int(time.time())-1,identifier))
    click(bot,confirm)
    assert 'expired' in last_text(bot)
    for value in ('entry-confirm:bad','entry-confirm:'+identifier+':bad','expense-delete:bad','expense-page:all:no'):
        click(bot,value)
    assert ledger.summary()['total']==12410


def test_draft_revision_direct_command_and_cancel(bot,ledger):
    command(bot,'/add 5 food Lunch');identifier=button(bot,'Confirm expense').split(':')[1]
    command(bot,f'/edit {identifier} 7 food Changed',update=2)
    assert 'S$7.00' in last_text(bot) and ledger.summary()['total']==12410
    command(bot,f'/cancel {identifier}',update=3)
    assert ledger.draft(identifier)['status']=='cancelled'


def test_atomic_parallel_confirmations_and_old_bot_upgrade_dedup(ledger):
    payload={'amount':'5','category':'food','note':'Lunch','spent_on':'2026-11-21'}
    identifier=ledger.propose_expense('add',payload,OWNER,OWNER,0,1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:ledger.confirm_expense_proposal(identifier,OWNER,OWNER,0,1),range(4)))
    assert len({result['expense']['id'] for result in results})==1
    assert ledger.summary()['total']==12910
    ledger.add_expense(payload,source='telegram',source_key='telegram:123:2')
    identifier=ledger.propose_expense('add',payload,OWNER,OWNER,0,2)
    assert ledger.draft(identifier)['status']=='saved'
    assert ledger.confirm_expense_proposal(identifier,OWNER,OWNER,0,1)['already_confirmed']
    assert ledger.summary()['total']==13410


def test_saved_add_button_cannot_resurrect_deleted_expense(bot,ledger):
    command(bot,'/add 5 food Lunch');confirm=button(bot,'Confirm expense');click(bot,confirm)
    ledger.delete_expense(4)
    click(bot,confirm)
    assert 'is deleted' in last_text(bot) and ledger.summary()['total']==12410


def test_cancel_vs_confirm_is_serialized(ledger):
    identifier=ledger.propose_expense('add',{'amount':'5','category':'food','spent_on':'2026-11-21'},OWNER,OWNER,0,1)
    def save():
        try:return ledger.confirm_expense_proposal(identifier,OWNER,OWNER,0,1)
        except ValueError:return None
    def cancel():
        try:ledger.cancel_telegram_draft(identifier,OWNER,OWNER,0,1)
        except ValueError:pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda fn:fn(),[save,cancel]))
    draft=ledger.draft(identifier)
    assert ledger.summary()['total']==(12910 if draft['status']=='saved' else 12410)


def test_expired_callback_ack_does_not_block_confirmed_action(bot,ledger,monkeypatch):
    command(bot,'/add 5 food Lunch');confirm=button(bot,'Confirm expense');original=bot.telegram
    def telegram(method,**payload):
        if method=='answerCallbackQuery':raise TelegramError('Expired query')
        return original(method,**payload)
    monkeypatch.setattr(bot,'telegram',telegram)
    click(bot,confirm)
    assert ledger.summary()['total']==12910


def test_legacy_pending_receipt_migrates_without_loss(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    payload={'amount':'5','category':'food','note':'Legacy receipt','spent_on':'2026-11-20'}
    identifier='f'*24
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE drafts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,expires INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',source_key TEXT UNIQUE)")
        db.execute('INSERT INTO drafts(id,kind,payload,expires) VALUES (?,?,?,?)',(identifier,'receipt',json.dumps(payload),int(time.time())+86400))
    migrated=Ledger(path)
    assert migrated.draft(identifier)['version']==1
    assert migrated.draft(identifier)['payload']['note']=='Legacy receipt'
    first=migrated.confirm_receipt(identifier,payload,expected_version=1)
    assert migrated.confirm_receipt(identifier,payload,expected_version=1)['id']==first['id']


def test_pending_submission_does_not_trigger_budget_alert(bot,ledger):
    from datetime import datetime
    from app.core import ZONE
    ledger.set_budget('2026-11','150')
    ledger.set_settings({'thresholds':[100],'renewal_reminders':False})
    now=datetime(2026,11,21,12,tzinfo=ZONE)
    command(bot,'/add 100 shopping Purchase')
    assert ledger.pending_notifications(now)==[]
    click(bot,button(bot,'Confirm expense'))
    assert '100%' in ledger.pending_notifications(now)[0]['text']
