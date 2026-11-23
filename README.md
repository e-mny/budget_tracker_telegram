# Penny

A private, self-hosted expense tracker with a desktop dashboard and a personal Telegram bot. Runs with Docker Compose and SQLite on a Linux VM in Proxmox, alongside the ACL rehab tracker. Dashboard defaults to port **8081**.

## Included

- Visual dashboard: category donut, daily/cumulative spending chart, totals, budget remaining, and searchable transactions.
- Set a monthly budget yourself. No suggested or prefilled budget. A saved budget carries forward until a later month's budget replaces it; earlier months retain their previous budget.
- Category budgets with per-category progress, and Telegram alerts at configurable budget thresholds (default 50%, 80%, 100%).
- CSV export and reviewed import with atomic validation and duplicate detection.
- Local receipt OCR on the dashboard or Telegram. Nothing is saved until you review and confirm.
- Savings goals with manual contributions, kept separate from expenditure.
- Visual month-to-month comparisons, using matching elapsed days for a month in progress.
- Subscription renewal reminders (default 3 days ahead, after 09:00), plus annual commitment and next billing dates.
- Travel currency records: original currency/amount and the actual SGD amount charged. No guessed exchange rate.
- GitHub Actions publishes a tested image on pushes to `main`; a separate Proxmox timer can install updates every five minutes.
- Add, edit, and delete expenses in the dashboard or Telegram. Telegram submissions, edits and deletions require confirmation.
- Recurring subscriptions with custom intervals in days, weeks, months, or years, auto-recorded on their due dates.
- Billing days initially **1**, editable in the dashboard. Entries begin in the month the database is first created. No earlier expenses are invented.
- A Telegram recap with a category chart at **23:59 on the last day of each month**, using **Asia/Singapore** time. Missed recaps are delivered after the worker comes back online.
- Password-protected dashboard and a Telegram allowlist for your private chat and one optional group, with owner-only commands.
- SGD throughout. Amounts stored in integer cents, with no currency conversion or silent decimal rounding.

## Start on Proxmox

Use a Linux VM with Docker Engine and the Docker Compose plugin installed. Copy this entire project directory to the VM. See [deployment notes](docs/deployment.md) for automatic GitHub updates, storage, and backups. Repository: [e-mny/budget_tracker_telegram](https://github.com/e-mny/budget_tracker_telegram).

```sh
cd money_tracker
cp .env.example .env
```

Edit `.env` and set a **unique dashboard password of at least 12 characters**. Then:

```sh
docker compose up -d --build
docker compose ps
```

Open **http://SERVER-LAN-IP:8081** and sign in with that password. Set your budget from **Monthly budget**, and set each subscription’s billing interval and first billing date from **Recurring payments**. The initial day-1 subscription charges are already recorded if day 1 has passed. If you need to correct the current month's dates or amounts, edit those rows in **Transactions** too; subscription changes preserve charges already recorded.

### Connect a new Telegram bot

1. Open the official **@BotFather** account in Telegram and use `/newbot`. Choose a different bot from your ACL tracker.
2. Copy the new token into `TELEGRAM_BOT_TOKEN` in `.env` on the server.
3. Set `TELEGRAM_ALLOWED_USER_ID` to your numeric personal Telegram user ID. If you already configured the ACL bot, reuse the **user ID**, not its token. The ACL bot's existing `/whoami` command can show your ID.
4. Uncomment `COMPOSE_PROFILES=telegram` in `.env`.
5. Run `docker compose up -d --build` again, then open the new bot's private chat and press **Start**. Telegram requires you to start the private chat before the bot can send you private messages.

Only the configured owner can read or add expenses, in their private chat or the one configured group. Other users, groups, channels, and anonymous group senders are ignored. Polling is outbound only; the bot needs no public webhook or inbound port. Telegram credentials are passed only to the worker, not to the browser.

### Use Penny in a group

1. Add the bot to your group. It needs permission to send text and photos; administrator access is not required.
2. From your own Telegram account, send `/whoami@YourBotUsername` in that group. This owner-only setup command works before the group is allowlisted and returns only user/chat/topic IDs.
3. Add the returned negative **Chat ID** to the server's existing `.env`, keeping your token and owner user ID:

```dotenv
TELEGRAM_ALLOWED_GROUP_ID=-1001234567890
TELEGRAM_NOTIFICATION_CHAT=group
# Optional: a forum topic ID from /whoami in that topic; 0 means general chat.
TELEGRAM_NOTIFICATION_THREAD_ID=0
```

4. Update the deployment files once, pull the published image, and recreate the Telegram worker. The new Compose variables must be present; an image update alone does not add them. For the image deployment:

```sh
git pull --ff-only
sudo docker compose -f compose.production.yaml pull telegram
sudo docker compose -f compose.production.yaml up -d --no-build telegram
```

For a source build, use `docker compose up -d --build telegram`. Use `/add@YourBotUsername 6.50 food Lunch`, `/month@YourBotUsername`, and `/recap@YourBotUsername` in the group. Commands reply in the same group/topic, and private-chat commands continue to work. Only your configured owner account may issue commands or use receipt buttons; other group members can see the bot's group responses.

Automatic recaps, budget alerts, and renewal reminders go to the configured group. `TELEGRAM_NOTIFICATION_CHAT=auto` (the default) chooses the group when `TELEGRAM_ALLOWED_GROUP_ID` is set and otherwise uses the owner's private chat. Set `private` to explicitly keep automatic messages private, or `group` to require a configured group. Scheduled group messages can use the optional notification topic; interactive replies always follow their incoming topic. Changing destination does not resend previously delivered alerts or recaps.

Keep Telegram privacy mode enabled. Address commands with `@YourBotUsername` when multiple bots share the group. For receipts, **reply to a message from Penny with your photo/image document**; the bot ignores unrelated group photos and conversation. You can also use a `/receipt@YourBotUsername` image caption if Telegram delivers it. Receipt corrections and confirmation buttons must be used in the draft's original chat and topic; existing private receipt drafts stay private. If Telegram upgrades a basic group to a supergroup, run `/whoami` again and update its changed group ID.

See [Telegram's group privacy rules](https://core.telegram.org/bots/features#privacy-mode).

### Bot commands


| Command                                             | What it does                                                        |
| ----------------------------------------------------- | --------------------------------------------------------------------- |
| `/add 6.50 food Chicken rice`                       | Preview a new food expense; confirm before saving                   |
| `/add 2.20 transport MRT`                           | Preview a transport expense                                         |
| `/add 45 personal_care Haircut @2026-11-20`         | Preview a past-dated expense                                        |
| `/whoami`                                           | Show your user, chat and topic IDs for setup                        |
| `/recent`                                           | Browse saved expenses with Edit/Delete buttons and pagination       |
| `/recent 2026-10`                                   | Browse a specific month's saved expenses                            |
| `/edit 42`                                          | Start a reply-based edit of expense #42                             |
| `/edit 42 7.50 food Corrected lunch`                | Preview replacement values for expense #42                          |
| `/delete 42`                                        | Review and explicitly confirm deletion of expense #42               |
| `/cancel DRAFT_ID`                                  | Discard a pending proposal without changing expenses                |
| `/categories`                                       | List category names and descriptions                                |
| `/month`                                            | Current month total, categories, and remaining budget               |
| `/recap`                                            | Send this month's recap and PNG chart now                           |
| `/recap 2026-10`                                    | Send a historical month's recap                                     |
| Send a receipt photo                                | Extract merchant, total, date, and category into a draft for review |
| `/receipt DRAFT_ID 12.50 food Merchant @2026-11-20` | Correct a receipt draft, then tap Save receipt                      |
| `/add 25 travel Lunch --original USD 18.50`         | Record the actual SGD charge and original foreign amount            |
| `/help`                                             | Show command help                                                   |

`/add` creates a durable pending proposal and shows **Confirm expense**, **Edit**, and **Cancel**. Nothing is recorded or counted towards budgets until you tap Confirm expense. Edit opens a reply prompt with the current entry to copy and change; your reply creates a new preview that must be confirmed again. Cancel discards the proposal. A confirmed entry returns its expense ID and Edit/Delete buttons.

Use `/recent` to find saved entries, including dashboard/CSV/receipt/automatic entries, or use `/edit ID` and `/delete ID` directly. **Edits show proposed and currently recorded values before Confirm changes. Deletions show the recorded entry before Confirm delete.** The amount, category, description, date and original currency can be changed from Telegram. An omitted edit date or original-currency flag keeps its recorded value; `--original none` removes the original currency. The supplied description replaces the old one (omit it to clear the description).

For example:

```text
/add 6.50 food Lunch
# Tap Edit, then reply: 7.50 food Corrected lunch
# Tap Confirm expense
/recent
/edit 42 8.50 food Corrected lunch @2026-11-20
# Tap Confirm changes
/delete 42
# Tap Confirm delete
```

Pending proposals and edit prompts survive restarts and expire after seven days. Buttons are restricted to your user, original chat and topic. Superseded previews are rejected. If an expense changes in the dashboard or another confirmed edit after you preview it, the bot asks you to review its latest values instead of overwriting the change. Repeated confirmations are idempotent. Pending entries do not affect spending, charts, or budget alerts. Recorded expense changes use the same ledger as the dashboard.

Amounts must be positive with at most two decimal places. Dates use the configured local timezone; future expenses are rejected. Messages delayed by a restart use the original Telegram message date unless you provide an explicit date.

Categories: `food` (**Food & drinks**), `transport` (**Transport**), `home` (**Home & bills**), `health` (**Health & protection**), `shopping` (**Shopping & personal**), `social` (**Leisure & social**), `travel` (**Travel**), and `other` (**Learning & other**). Earlier category names remain accepted as aliases. Upgrading maps old expenses and recurring payments into these eight categories without changing amounts, dates, or duplicate-protection keys. Insurance and fitness join health; personal care joins shopping; entertainment and gifts join social; education joins other. Phone bills join home, gym payments join health, and Netflix joins social. Category limits are merged using their carried-forward values; pending CSV previews affected by the change need to be uploaded and reviewed again.

The recurring setup includes **S$401.20 household allowance on the 25th** under Home & bills, and **S$600 investments on the 4th**, starting in the month this setup is installed. Investment contributions are recorded separately and appear in **Investing steadily** and Telegram summaries; they do not use the expense budget or affect category totals, spending comparisons, or expense CSV exports. Both schedules can be edited or paused under **Recurring payments**. Setup runs once and preserves later edits. Already-due dates in the setup month are recorded on first sync; earlier months are not backfilled.

Automatic recurring expense entries already count towards the total and budget. **Do not submit those same charges again through `/add`.** Pausing a subscription stops future unrecorded charges and retains history. Set the full amount paid per renewal, a repeat interval (for example, every 3 months or every 1 year), and the first billing date in the dashboard. Existing subscriptions keep their monthly schedule after upgrading. Dates 29–31 are clamped to the last day of shorter months; a February 29 annual plan returns to February 29 in leap years. Monthly equivalents and annual commitments are estimates at current rates (day/week intervals use 365 days per year); spending records the full charge only when due. Schedule edits and resuming a paused subscription apply from today without creating older charges. New subscriptions catch up from their first billing date. Deleting an automatic entry removes it from spending and prevents that charge from being recreated. Future recurring charges continue; change the subscription settings in the dashboard to pause future billing.

## Monthly recap behavior

Keep the Telegram worker running with internet access. It checks the local calendar after each polling batch. At 23:59 on the final calendar day, it sends one photo containing a chart and a caption with totals, budget status, and category amounts. The chart is generated locally; no third-party chart service receives your ledger.

The recap is a snapshot at dispatch time. Expenses entered afterwards, including later in that final minute, are visible in the dashboard and an on-demand `/recap`; the automatic recap is not reissued. The worker drains queued Telegram messages before sending automatic recaps. Recap delivery state and polling offsets survive restarts. A missed completed-month recap is retried after downtime. Rarely, a crash after Telegram accepts a photo but before the local delivery marker commits can cause a duplicate recap; Telegram provides no general exactly-once send key. Recorded expense submissions and recurring subscription charges have durable deduplication keys.

This tracks **recorded expenditure**, not bank account balances. Automatic charges represent your configured billing schedule, not confirmation from your bank.

## Configuration


| Variable                          | Default / purpose                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------ |
| `DASHBOARD_PASSWORD`              | Required; at least 12 characters. Changing it invalidates existing dashboard sessions.         |
| `PORT`                            | `8082`                                                                                         |
| `BIND_ADDRESS`                    | `0.0.0.0`; use the VM's LAN IP or `127.0.0.1` as appropriate                                   |
| `TZ`                              | `Asia/Singapore`; used by both services for dates, recurring charges, and recaps               |
| `COOKIE_SECURE`                   | `false` for trusted LAN HTTP; set `true` when using HTTPS                                      |
| `TELEGRAM_BOT_TOKEN`              | New bot's BotFather token                                                                      |
| `TELEGRAM_ALLOWED_USER_ID`        | Numeric personal Telegram account ID; only this owner may use the ledger                       |
| `TELEGRAM_ALLOWED_GROUP_ID`       | Optional negative numeric group/supergroup ID; private access remains available                |
| `TELEGRAM_NOTIFICATION_CHAT`      | `auto`: configured group if present, otherwise private; explicit `group` / `private` overrides |
| `TELEGRAM_NOTIFICATION_THREAD_ID` | `0`; optional positive group topic ID for automatic messages                                   |
| `COMPOSE_PROFILES`                | Set `telegram` to run the optional worker                                                      |
| `DATA_DIR`                        | Local development only; Docker uses `/data`                                                    |

For external access, use your VPN or an HTTPS reverse proxy. If using a reverse proxy, preserve `Host`, forward the correct scheme, and configure Uvicorn's trusted proxy addresses to match only that proxy. Do not publish plain HTTP credentials to the internet. The dashboard is a single-owner journal with a 30-day HTTP-only session cookie and rate-limited login. `.env` and runtime data are ignored by Git and excluded from Docker images.

## Local development and verification

Python 3.12+ and the Tesseract command-line program with English language data for receipt OCR; no Node build or CDN is required for the dashboard. Docker installs OCR automatically. On macOS use `brew install tesseract`; on Debian/Ubuntu use `sudo apt-get install tesseract-ocr tesseract-ocr-eng`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
# Set DASHBOARD_PASSWORD in your shell (the 12+ character password stays local).
.venv/bin/uvicorn app.web:create_app --factory --host 127.0.0.1 --port 8081
```

The Python process uses environment variables; it does not automatically read `.env`. Docker Compose reads `.env` for deployment. Development data lives in `./data/`.

```sh
.venv/bin/python -m pytest tests -q
node --check app/static/app.js
docker compose config --quiet
```

Tests use isolated temporary databases and a fixed calendar. They cover exact money, date validation, monthly budgets, recurrence and shorter months, duplicate protection, recap schedules and retries, chart generation, Telegram restrictions, authentication, and expense editing.

## The added tools

**Cash flow and income:** Add income with a source name, actual amount received, date, and optional description. Income entries can be edited or deleted. For regular pay, add a recurring payment with type **Income**; only due receipts count, and deleting an automatic receipt keeps its duplicate-protection key. The Whole picture panel groups income by source and shows net cash flow as recorded income minus expenses minus investment contributions. It is not a bank balance; missing income or expenses make the picture incomplete. Income does not change expense budgets or expenditure comparisons. No salary or income amount is prefilled.

**Spending priorities:** Necessity is separate from the eight expense categories. Expenses and recurring expenses can be marked **Necessity**, **Non-necessity**, or **Unclassified** and filtered in Transactions. Gym, insurance, phone bills, and household contributions default to necessities, according to your priorities. Other spending stays unclassified until you choose. Use default restores automatic classification; explicit choices take precedence. Changes to recurring priorities affect future charges; existing charges can be edited separately. Income and investments are excluded from this spending split. Expense CSVs contain the existing expense fields; use a database backup to preserve income and spending priorities.

**Category budgets and notifications:** Set category limits from the category budget panel. Limits carry forward, and a blank limit removes it from the selected month onward. Limits are independent of the total monthly budget; their sum is not constrained to it. Notification settings control both total and category threshold alerts, reminder switches, and reminder lead time. Budget checks run after each Telegram poll. Thresholds send once per category/month; if spending crosses several at once, one message covers them. Late historical changes update the dashboard without sending old-month budget alerts. Notifications require the Telegram worker and either a started private chat or the configured group destination. As with recaps, a crash after Telegram accepts a message but before its local marker commits can rarely cause a duplicate.

**CSV:** Use the Receipts & data panel to export this month or all records. Import UTF-8 CSV with `spent_on,amount,category,note,original_currency,original_amount` headers (the last three are optional). Dates are YYYY-MM-DD, and `amount` is the actual SGD charge. Maximum 2 MB / 5,000 rows. The preview validates every row before saving and shows the first 100; confirmation imports all valid file rows atomically. Exact matching records, including deleted records, are skipped; multiple identical rows in a new file retain their multiplicity. Exported descriptions are escaped against spreadsheet formulas and can be imported back. CSV exports contain expenses, not budgets, goals, settings, or passwords; use a volume backup for those.

**Receipts:** Upload a JPG, PNG or WebP (10 MB / 25 MP maximum) in the dashboard or send it to the bot. Tesseract runs locally, using English OCR; low-quality or non-English receipts may need manual corrections. Review the merchant, amount, date, and category. The dashboard lets you edit all fields before Save expense; the bot provides Edit and Save receipt buttons, or `/receipt DRAFT_ID ...` / `/edit DRAFT_ID ...` for corrections. Each revision has a new confirmation; old buttons cannot save newer values unseen. Foreign receipts require the actual SGD charge before saving. Photos are used temporarily for OCR, then removed; review text and drafts expire after seven days. Receipt extraction is a suggestion and does not confirm a bank charge.

**Savings:** Create a goal with a target, optional date, and already-saved amount; record contributions or edit the saved total. These are manual progress records and never add an expense or change your spending budget.

**Comparisons:** The current month is compared with the same elapsed days of the previous month, capped at its length. Completed months use full-month totals. Months before tracking began are identified as missing history; imported historical records can supply it.

**Travel currencies:** Use the optional foreign-currency fields in Add expense, or `/add 25 travel Lunch --original USD 18.50`. Totals, budgets, and charts use only the actual SGD charge. Common travel currencies are supported, including JPY/KRW/VND with no fractional units and KWD/BHD/OMR with three. No exchange-rate API, conversion estimate, or market rate is used.

[Feature review status](docs/suggestions.md): ideas 1–8 are implemented; split expenses/reimbursements remain a suggestion.

Implementation references: [Telegram Bot API](https://core.telegram.org/bots/api), [FastAPI security](https://fastapi.tiangolo.com/tutorial/security/), and [SQLite WAL](https://sqlite.org/wal.html).
