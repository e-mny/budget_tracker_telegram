"""Telegram routing and allowlists without real network or a live bot token."""
from datetime import date, datetime

import httpx
import pytest

from app import core
from app.bot import Bot, TelegramError
from app.core import Ledger, ZONE
from app.receipts import parse_receipt

OWNER=123
GROUP=-1001234567890
BOT_ID=456


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'today',lambda: date(2026,11,21))
    return Ledger(tmp_path/'ledger.sqlite3')


def make_bot(ledger,monkeypatch,**settings):
    bot=Bot(ledger,'test-token',OWNER,group_id=GROUP,**settings)
    bot.username='PennyBot'
    bot.bot_id=BOT_ID
    calls=[]
    monkeypatch.setattr(bot,'telegram',lambda method,files=None,**payload: calls.append((method,payload,files)) or {})
    return bot,calls


def message(text='/month@PennyBot', user=OWNER, chat=GROUP, kind='supergroup', topic=7, identifier=1, **extra):
    return {'update_id':identifier,'message':{'text':text,'from':{'id':user},'chat':{'id':chat,'type':kind},
                                            **({'message_thread_id':topic} if topic else {}), **extra}}


@pytest.mark.parametrize('kind',['group','supergroup'])
def test_group_add_reply_and_dedup(ledger,monkeypatch,kind):
    bot,calls=make_bot(ledger,monkeypatch)
    update=message('/add@PennyBot 6.50 food Lunch',kind=kind)
    bot.process_update(update);bot.process_update(update)
    assert ledger.summary()['total']==13060
    assert len(calls)==2
    assert all(call[1]['chat_id']==GROUP and call[1]['message_thread_id']==7 for call in calls)
    assert 'Saved #' in calls[0][1]['text']


@pytest.mark.parametrize('user,chat,kind',[(999,GROUP,'supergroup'),(OWNER,-100999,'supergroup'),(OWNER,GROUP,'channel'),(999,999,'private')])
def test_other_users_chats_and_channels_are_ignored(ledger,monkeypatch,user,chat,kind):
    bot,calls=make_bot(ledger,monkeypatch)
    bot.process_update(message('/add@PennyBot 6.50 food Lunch',user=user,chat=chat,kind=kind))
    assert calls==[] and ledger.summary()['total']==12410


def test_private_chat_still_works_with_group_configured(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    bot.process_update(message('/add 2.20 transport MRT',chat=OWNER,kind='private',topic=0))
    assert ledger.summary()['total']==12630
    assert calls[0][1]['chat_id']==OWNER and 'message_thread_id' not in calls[0][1]


def test_group_chatter_other_commands_and_other_bots_are_ignored(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    for text in ('Hello everyone','/add@OtherBot 6.50 food Lunch','/help@OtherBot','/unrelated'):
        bot.process_update(message(text))
    other_bot_reply={'reply_to_message':{'from':{'id':777,'is_bot':True}}}
    bot.process_update(message('/add 6.50 food Lunch',**other_bot_reply))
    bot.process_update(message('/add@PennyBot 6.50 food Lunch',sender_chat={'id':GROUP}))
    assert calls==[] and ledger.summary()['total']==12410
    bot.process_update(message('/unrelated@PennyBot'))
    assert len(calls)==1 and 'Unknown command' in calls[0][1]['text']


def test_case_insensitive_bare_group_commands_and_error_routing(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    bot.process_update(message('/MONTH@pennybot'))
    assert 'Total spent:' in calls[-1][1]['text']
    bot.process_update(message('/add 1.001 food Invalid'))
    assert 'two decimal places' in calls[-1][1]['text']
    assert calls[-1][1]['chat_id']==GROUP and calls[-1][1]['message_thread_id']==7


def test_whoami_owner_can_discover_unconfigured_group_without_reading_ledger(ledger,monkeypatch):
    bot=Bot(ledger,'test-token',OWNER)
    bot.username='PennyBot';calls=[]
    monkeypatch.setattr(bot,'telegram',lambda method,**payload: calls.append((method,payload)))
    bot.process_update(message('/whoami@PennyBot',chat=-987654,topic=42))
    assert len(calls)==1
    assert 'Chat ID: -987654' in calls[0][1]['text'] and 'Topic ID: 42' in calls[0][1]['text']
    assert 'Total spent' not in calls[0][1]['text']
    calls.clear()
    bot.process_update(message('/whoami@PennyBot',user=999))
    bot.process_update(message('/month@PennyBot'))
    assert calls==[]


def test_manual_recap_stays_in_group_topic(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    monkeypatch.setattr('app.bot.render_summary',lambda summary:b'png')
    bot.process_update(message('/recap@PennyBot 2026-11'))
    assert calls[0][0]=='sendPhoto'
    assert calls[0][1]['chat_id']==GROUP and calls[0][1]['message_thread_id']==7
    assert calls[0][2]['photo'][1]==b'png'


def test_photo_privacy_and_captions_for_other_bots(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    received=[];monkeypatch.setattr(bot,'receive_receipt',lambda update_id,payload:received.append((update_id,payload)))
    photo={'photo':[{'file_id':'image'}]}
    bot.process_update(message('',**photo))
    bot.process_update(message('',caption='/receipt@OtherBot',**photo))
    bot.process_update(message('',reply_to_message={'from':{'id':777,'is_bot':True}},**photo))
    assert not received
    bot.process_update(message('',reply_to_message={'from':{'id':BOT_ID,'is_bot':True}},**photo))
    bot.process_update(message('',caption='/receipt@PennyBot',identifier=2,**photo))
    assert len(received)==2 and calls==[]


def test_receipt_upload_binds_preview_to_chat_topic(ledger,monkeypatch):
    def transport(request):
        return httpx.Response(200,content=b'synthetic receipt image')
    client=httpx.Client(transport=httpx.MockTransport(transport))
    bot,calls=make_bot(ledger,monkeypatch,client=client)
    real_telegram=bot.telegram
    def telegram(method,**payload):
        if method=='getFile':return {'file_path':'photos/receipt.jpg'}
        return real_telegram(method,**payload)
    monkeypatch.setattr(bot,'telegram',telegram)
    monkeypatch.setattr('app.bot.extract_receipt',lambda content:parse_receipt('CAFE\nTOTAL SGD 5.00'))
    update=message('',photo=[{'file_id':'receipt','file_size':100}],reply_to_message={'from':{'id':BOT_ID,'is_bot':True}})
    bot.process_update(update)
    assert len(calls)==1 and calls[0][1]['chat_id']==GROUP and calls[0][1]['message_thread_id']==7
    keyboard=calls[0][1]['reply_markup']['inline_keyboard'][0]
    identifier=keyboard[0]['callback_data'].split(':',1)[1]
    payload=ledger.draft(identifier,'receipt')['payload']
    assert payload['_telegram_chat_id']==GROUP and payload['_telegram_thread_id']==7
    assert ledger.summary()['total']==12410
    client.close()


def callback(identifier,user=OWNER,chat=GROUP,topic=7,action='receipt-save'):
    return {'callback_query':{'id':'query1','from':{'id':user},'data':action+':'+identifier,
                             'message':{'chat':{'id':chat,'type':'private' if chat==OWNER else 'supergroup'},'message_thread_id':topic}}}


def test_group_receipt_callbacks_authorization_context_and_dedup(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    payload={**parse_receipt('CAFE\nTOTAL SGD 5.00'),'_telegram_chat_id':GROUP,'_telegram_thread_id':7}
    identifier=ledger.new_draft('receipt',payload)
    bot.process_update(callback(identifier,user=999))
    bot.process_update(callback(identifier,chat=-999))
    assert calls==[] and ledger.summary()['total']==12410
    bot.process_update(callback(identifier,topic=8))
    assert 'original chat and topic' in calls[-1][1]['text']
    assert ledger.summary()['total']==12410
    bot.process_update(callback(identifier));bot.process_update(callback(identifier))
    assert ledger.summary()['total']==12910
    assert calls[-1][1]['chat_id']==GROUP and calls[-1][1]['message_thread_id']==7


def test_private_legacy_receipt_cannot_be_exposed_or_cancelled_in_group(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    identifier=ledger.new_draft('receipt',parse_receipt('PRIVATE MERCHANT\nTOTAL SGD 5.00'))
    bot.process_update(message(f'/receipt@PennyBot {identifier} 6 food Something'))
    assert 'original chat and topic' in calls[-1][1]['text']
    assert all('PRIVATE MERCHANT' not in c[1].get('text','') for c in calls)
    bot.process_update(callback(identifier,action='receipt-cancel'))
    assert ledger.draft(identifier,'receipt')['status']=='pending'
    bot.process_update(callback(identifier,chat=OWNER,topic=0))
    assert ledger.summary()['total']==12910


def test_group_receipt_edit_and_cancel(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    identifier=ledger.new_draft('receipt',{**parse_receipt('CAFE\nTOTAL SGD 5.00'),'_telegram_chat_id':GROUP,'_telegram_thread_id':7})
    bot.process_update(message(f'/receipt@PennyBot {identifier} 6.50 food Corrected receipt'))
    assert ledger.draft(identifier,'receipt')['payload']['amount']=='6.50'
    assert calls[-1][1]['chat_id']==GROUP and 'Corrected receipt' in calls[-1][1]['text']
    bot.process_update(callback(identifier,action='receipt-cancel'))
    assert ledger.draft(identifier,'receipt')['status']=='cancelled'
    assert ledger.summary()['total']==12410


def test_scheduled_group_messages_and_recaps_use_configured_topic_once(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch,notification_chat='group',notification_thread_id=23)
    ledger.set_settings({'renewal_reminders':False})
    ledger.set_budget('2026-11','100')
    monkeypatch.setattr('app.bot.render_summary',lambda summary:b'png')
    now=datetime(2026,11,30,23,59,tzinfo=ZONE)
    bot.tick(now);bot.tick(now)
    assert [c[0] for c in calls]==['sendMessage','sendPhoto']
    assert all(c[1]['chat_id']==GROUP and c[1]['message_thread_id']==23 for c in calls)


def test_notifications_default_to_configured_group_with_private_override(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    ledger.set_settings({'renewal_reminders':False});ledger.set_budget('2026-11','100')
    bot.tick(datetime(2026,11,21,12,tzinfo=ZONE))
    assert calls[0][1]['chat_id']==GROUP and 'message_thread_id' not in calls[0][1]
    private,calls=make_bot(ledger,monkeypatch,notification_chat='private')
    assert private.notification_destination()=={}
    private.send('private',**private.notification_destination())
    assert calls[-1][1]['chat_id']==OWNER
    assert Bot(ledger,'test-token',OWNER).notification_destination()=={}


def test_renewal_reminders_route_to_group(ledger,monkeypatch):
    bot,calls=make_bot(ledger,monkeypatch)
    monkeypatch.setattr(core,'today',lambda:date(2026,11,29))
    bot.tick(datetime(2026,11,29,9,tzinfo=ZONE))
    assert len(calls)==3 and all(c[1]['chat_id']==GROUP for c in calls)
    assert all('subscription renewal' in c[1]['text'] for c in calls)


@pytest.mark.parametrize('settings',[{'group_id':'123'}, {'group_id':'-0'}, {'group_id':'-1.2'},
    {'notification_chat':'group'}, {'notification_chat':'anywhere'},
    {'notification_thread_id':-1}, {'notification_thread_id':1.5},
    {'group_id':GROUP,'notification_chat':'private','notification_thread_id':7}])
def test_invalid_group_settings_fail_closed(ledger,settings):
    with pytest.raises(ValueError):Bot(ledger,'test-token',OWNER,**settings)
