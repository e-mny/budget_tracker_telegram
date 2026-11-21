"""Private-chat Telegram worker. Shared SQLite ledger; outbound polling only."""
import logging
import os
import re
import signal
import time
from datetime import datetime, timedelta

import httpx

from .core import CATEGORIES, Ledger, ZONE, money, month_bounds, parse_add
from .graphs import render_summary
from .receipts import MAX_RECEIPT, extract_receipt

log = logging.getLogger('penny-bot')
HELP = '''Penny · your expense ledger

/add 6.50 food Chicken rice
/add 2.20 transport MRT
/add 45 personal_care Haircut @2026-11-20
/categories — available categories
/month — spending and budget this month
/recap — visual monthly summary
/recap YYYY-MM — a previous month
/receipt ID 12.50 food Lunch — revise a receipt draft
Send a receipt photo — extract fields for review
/add 25 travel Lunch --original USD 18.50 — travel purchase
/help — show these commands

Amounts are SGD. Date is optional and uses Asia/Singapore by default. Each /add saves immediately; edit or delete mistakes in the dashboard. Automatic subscriptions are already included; do not log them twice.'''


def summary_text(summary):
    lines = [f"Penny · {summary['month']}", f"Total spent: {money(summary['total'])} · {summary['count']} expenses"]
    if summary['budget'] is not None:
        lines.append(f"Budget: {money(summary['budget'])}")
        lines.append(f"{'Remaining' if summary['remaining'] >= 0 else 'Over budget'}: {money(abs(summary['remaining']))}")
    else:
        lines.append('Monthly budget: not set. Set it in the dashboard.')
    for cat in sorted(summary['categories'], key=lambda c: c['amount'], reverse=True):
        if cat['amount']:
            lines.append(f"{cat['label']}: {money(cat['amount'])} ({cat['amount'] / summary['total']:.0%})")
    lines.append(f"Recorded expenses · SGD · {summary['timezone']}")
    return '\n'.join(lines)


def due_recaps(now, start_month):
    """On the last day after 23:59, plus unsent completed months after downtime."""
    now = now.astimezone(ZONE)
    cursor, _ = month_bounds(start_month)
    current = now.date().replace(day=1)
    result = []
    while cursor < current:
        result.append(cursor.strftime('%Y-%m'))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    _, last = month_bounds(now.strftime('%Y-%m'))
    if now.date() == last and now.hour == 23 and now.minute >= 59:
        result.append(now.strftime('%Y-%m'))
    return result


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, ledger, token, user_id, client=None):
        if not token or not str(user_id).isdigit() or int(user_id) <= 0:
            raise ValueError('Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID (positive numeric private user ID).')
        self.ledger = ledger
        self.token = token
        self.user_id = int(user_id)
        self.username = ''
        self.client = client or httpx.Client(timeout=45)
        self.running = True

    def telegram(self, method, files=None, **payload):
        try:
            response = self.client.post(f'https://api.telegram.org/bot{self.token}/{method}',
                                        **({'data': payload, 'files': files} if files else {'json': payload}))
            if not response.is_success:
                raise TelegramError('Telegram request failed.')
            body = response.json()
            if not body.get('ok'):
                raise TelegramError('Telegram request failed.')
            return body['result']
        except (httpx.HTTPError, ValueError):
            # HTTP exception strings contain the credential-bearing Telegram URL. Never log them.
            raise TelegramError('Telegram connection failed.') from None

    def send(self, text):
        return self.telegram('sendMessage', chat_id=self.user_id, text=text)

    def send_recap(self, month):
        summary = self.ledger.summary(month)
        content = render_summary(summary)
        # A single photo+caption is one API call, avoiding a half-sent text/chart pair.
        self.telegram('sendPhoto', files={'photo': ('monthly-recap.png', content, 'image/png')},
                      chat_id=self.user_id, caption=summary_text(summary))

    def receipt_preview(self, identifier):
        draft = self.ledger.draft(identifier, 'receipt')
        payload = draft['payload']
        valid = True
        try:
            self.ledger.validate_expense(payload)
        except ValueError:
            valid = False
        lines = [f'Receipt draft {identifier} · {draft["status"]}',
                 f'Merchant: {payload.get("note") or "Not detected"}',
                 f'SGD amount: {payload.get("amount") or "Enter the SGD amount charged"}',
                 f'Category: {payload.get("category", "other")}', f'Date: {payload.get("spent_on")}',
                 payload.get('warning', 'Review all fields before saving.')]
        if payload.get('original_currency'):
            lines.append(f'Original: {payload["original_currency"]} {payload.get("original_amount") or "Enter original amount"}')
        lines.append(f'To correct: /receipt {identifier} 12.50 food Merchant @YYYY-MM-DD')
        lines.append('For foreign purchases append: --original USD 10.00')
        keyboard = []
        if valid and draft['status'] == 'pending':
            keyboard.append({'text':'Save receipt','callback_data':'receipt-save:'+identifier})
        if draft['status'] == 'pending':
            keyboard.append({'text':'Cancel','callback_data':'receipt-cancel:'+identifier})
        self.telegram('sendMessage', chat_id=self.user_id, text='\n'.join(lines),
                      reply_markup={'inline_keyboard':[keyboard]} if keyboard else {'inline_keyboard':[]})

    def receive_receipt(self, update_id, message):
        photos = message.get('photo') or []
        document = message.get('document')
        photo = photos[-1] if photos else document
        if not photo or (document and document.get('mime_type') not in ('image/jpeg','image/png','image/webp')):
            raise ValueError('Send the receipt as a JPG, PNG or WebP photo or image document.')
        if photo.get('file_size', 0) > MAX_RECEIPT:
            raise ValueError('Receipt images must be at most 10 MB.')
        remote = self.telegram('getFile', file_id=photo['file_id'])
        file_path = remote.get('file_path', '')
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', file_path) or '..' in file_path:
            raise TelegramError('Invalid Telegram file path.')
        content = bytearray()
        try:
            with self.client.stream('GET', f'https://api.telegram.org/file/bot{self.token}/{file_path}') as response:
                if not response.is_success:
                    raise TelegramError('Could not download receipt.')
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RECEIPT:
                        raise ValueError('Receipt images must be at most 10 MB.')
        except httpx.HTTPError:
            raise TelegramError('Receipt download failed.') from None
        payload = extract_receipt(bytes(content))
        if message.get('date'):
            # If OCR did not identify a date, preserve the date of a delayed upload.
            from .core import today
            if payload['spent_on'] == today().isoformat():
                payload['spent_on'] = datetime.fromtimestamp(message['date'], ZONE).date().isoformat()
        identifier = self.ledger.new_draft('receipt', payload, f'receipt-upload:{self.user_id}:{update_id}')
        self.receipt_preview(identifier)

    def process_callback(self, callback):
        message = callback.get('message', {})
        if callback.get('from', {}).get('id') != self.user_id or message.get('chat', {}).get('id') != self.user_id or message.get('chat', {}).get('type') != 'private':
            return
        self.telegram('answerCallbackQuery', callback_query_id=callback['id'])
        action, _, identifier = callback.get('data', '').partition(':')
        try:
            if action == 'receipt-save':
                draft = self.ledger.draft(identifier, 'receipt')
                row = self.ledger.confirm_receipt(identifier, draft['payload'])
                self.send(f'Saved receipt #{row["id"]} · {money(row["amount"])}. Edit any mistakes in the dashboard.')
            elif action == 'receipt-cancel':
                self.ledger.cancel_draft(identifier)
                self.send('Receipt draft cancelled.')
        except (ValueError, LookupError) as error:
            self.send(str(error))

    def process_update(self, update):
        if 'callback_query' in update:
            return self.process_callback(update['callback_query'])
        message = update.get('message', {})
        if (message.get('from', {}).get('id') != self.user_id or
            message.get('chat', {}).get('id') != self.user_id or
            message.get('chat', {}).get('type') != 'private'):
            return
        if message.get('photo') or message.get('document'):
            try:
                self.receive_receipt(update['update_id'], message)
            except (ValueError, LookupError) as error:
                self.send(str(error))
            return
        text = message.get('text', '')
        match = re.match(r'^/([a-z_]+)(?:@([a-zA-Z0-9_]+))?(?:\s+([\s\S]*))?$', text)
        if not match:
            self.send('Use /add 6.50 food Lunch to log an expense, or /help for commands.')
            return
        command, address, args = match.groups()
        if address and address.lower() != self.username.lower():
            return
        args = args or ''
        try:
            if command in ('start', 'help'):
                self.send(HELP)
            elif command == 'categories':
                self.send('Categories (use the name before the colon):\n\n' + '\n'.join(f'{key}: {label}' for key, (label, _) in CATEGORIES.items()))
            elif command == 'add':
                # Telegram timestamps keep an update received after midnight in its original month.
                local_date = datetime.fromtimestamp(message['date'], ZONE).date() if message.get('date') else None
                row = self.ledger.add_expense(parse_add(args, as_of=local_date), source='telegram',
                                               source_key=f"telegram:{self.user_id}:{update['update_id']}")
                state = self.ledger.summary(row['spent_on'][:7])
                self.send(f"Saved #{row['id']} · {money(row['amount'])} · {CATEGORIES[row['category']][0]}\n"
                          f"{row['note'] or 'No description'} · {row['spent_on']}\nMonth total: {money(state['total'])}\n"
                          + (f"Budget remaining: {money(state['remaining'])}" if state['budget'] else 'Set your monthly budget in the dashboard.'))
            elif command == 'receipt':
                identifier, separator, corrected = args.strip().partition(' ')
                if not separator:
                    raise ValueError('Send a receipt photo, then use /receipt DRAFT_ID 12.50 food Merchant to correct its fields before saving.')
                draft = self.ledger.draft(identifier, 'receipt')
                if draft['status'] != 'pending':
                    raise ValueError('This receipt draft has already been saved or cancelled.')
                payload = parse_add(corrected)
                self.ledger.validate_expense(payload)
                revised = {**draft['payload'], **payload,
                           'original_currency': payload.get('original_currency'), 'original_amount': payload.get('original_amount')}
                import json
                with self.ledger.db() as db:
                    db.execute("UPDATE drafts SET payload=? WHERE id=? AND status='pending'",(json.dumps(revised), identifier))
                self.receipt_preview(identifier)
            elif command == 'month':
                self.send(summary_text(self.ledger.summary()))
            elif command == 'recap':
                self.send_recap(args.strip() or datetime.now(ZONE).strftime('%Y-%m'))
            else:
                self.send('Unknown command. Use /help to see the available commands.')
        except (ValueError, LookupError) as error:
            self.send(str(error))

    def tick(self, now=None):
        now = (now or datetime.now(ZONE)).astimezone(ZONE)
        self.ledger.sync_subscriptions(now.date())
        with self.ledger.db() as db:
            start = db.execute("SELECT value FROM meta WHERE key='start_month'").fetchone()['value']
            sent = {row['month'] for row in db.execute('SELECT month FROM recaps')}
        for notification in self.ledger.pending_notifications(now):
            self.send(notification['text'])
            self.ledger.mark_notification(notification['keys'], now)
        for month in due_recaps(now, start):
            if month in sent:
                continue
            self.send_recap(month)
            with self.ledger.db() as db:
                db.execute('INSERT OR IGNORE INTO recaps VALUES (?,?)', (month, now.isoformat()))

    def stop(self, *_):
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while self.running:
            try:
                if not self.username:
                    self.username = self.telegram('getMe')['username']
                    self.telegram('setMyCommands', commands=[{'command': c, 'description': d} for c, d in [
                        ('add', 'Log an expense: /add 6.50 food Lunch'), ('month', 'Current month spending and budget'),
                        ('recap', 'Visual monthly summary'), ('receipt', 'Correct a receipt draft before saving'), ('categories', 'Available expense categories'), ('help', 'How to use Penny')]])
                updates = self.telegram('getUpdates', offset=int(self.ledger.get_state('offset', '0')),
                                        timeout=20, allowed_updates=['message', 'callback_query'])
                for update in updates:
                    self.process_update(update)
                    self.ledger.set_state('offset', update['update_id'] + 1)
                # Drain pending messages before recapping; delayed month-end entries belong in the recap.
                if len(updates) < 100:
                    self.tick()
            except TelegramError:
                log.warning('Telegram unavailable. Retrying in 10 seconds; check token and outbound connection if this persists.')
                for _ in range(10):
                    if not self.running:
                        break
                    time.sleep(1)
            except Exception as error:
                # Log only class names, never URLs, credentials or expense content.
                log.error('Worker error (%s). Retrying in 10 seconds.', type(error).__name__)
                for _ in range(10):
                    if not self.running:
                        break
                    time.sleep(1)
        self.client.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        bot = Bot(Ledger(), os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_ALLOWED_USER_ID', ''))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    bot.run()


if __name__ == '__main__':
    main()
