# Penny

A private, self-hosted expense tracker with a desktop dashboard and a personal Telegram bot. Runs with Docker Compose and SQLite on a Linux VM in Proxmox, alongside the ACL rehab tracker. Dashboard defaults to port **8081**.

## Included

- Visual dashboard: category donut, daily/cumulative spending chart, totals, budget remaining, and searchable transactions.
- Set a monthly budget yourself. No suggested or prefilled budget. A saved budget carries forward until a later month's budget replaces it; earlier months retain their previous budget.
- Add, edit, and delete expenses in the dashboard, or submit categorized expenses through Telegram.
- Monthly recurring subscriptions, auto-recorded on the billing day: **gym S$98**, **phone S$10.10**, **Netflix S$16**. Total **S$124.10/month**.
- Billing days initially **1**, editable in the dashboard. Entries begin in the month the database is first created. No earlier expenses are invented.
- A Telegram recap with a category chart at **23:59 on the last day of each month**, using **Asia/Singapore** time. Missed recaps are delivered after the worker comes back online.
- Password-protected dashboard and a Telegram allowlist restricted to your private chat.
- SGD throughout. Amounts stored in integer cents, with no currency conversion or silent decimal rounding.

## Start on Proxmox

Use a Linux VM with Docker Engine and the Docker Compose plugin installed. Copy this entire project directory to the VM. See [deployment notes](docs/deployment.md) for updates, storage, and backups.

```sh
cd money_tracker
cp .env.example .env
```

Edit `.env` and set a **unique dashboard password of at least 12 characters**. Then:

```sh
docker compose up -d --build
docker compose ps
```

Open **http://SERVER-LAN-IP:8081** and sign in with that password. Set your budget from **Monthly budget**, and set the actual subscription billing days from **Subscriptions**. The initial day-1 subscription charges are already recorded if day 1 has passed. If you need to correct the current month's dates or amounts, edit those rows in **Transactions** too; subscription changes preserve charges already recorded.

### Connect a new Telegram bot

1. Open the official **@BotFather** account in Telegram and use `/newbot`. Choose a different bot from your ACL tracker.
2. Copy the new token into `TELEGRAM_BOT_TOKEN` in `.env` on the server.
3. Set `TELEGRAM_ALLOWED_USER_ID` to your numeric personal Telegram user ID. If you already configured the ACL bot, reuse the **user ID**, not its token. The ACL bot's existing `/whoami` command can show your ID.
4. Uncomment `COMPOSE_PROFILES=telegram` in `.env`.
5. Run `docker compose up -d --build` again, then open the new bot's private chat and press **Start**. Telegram requires you to start the chat before the bot can send recaps.

Only that user in a private chat can read or add expenses. Group chats and other senders are ignored. Polling is outbound only; the bot needs no public webhook or inbound port. Telegram credentials are passed only to the worker, not to the browser.

### Bot commands

| Command | What it does |
| --- | --- |
| `/add 6.50 food Chicken rice` | Save a food expense today |
| `/add 2.20 transport MRT` | Save a transport expense |
| `/add 45 personal_care Haircut @2026-11-20` | Save a past-dated expense |
| `/categories` | List category names and descriptions |
| `/month` | Current month total, categories, and remaining budget |
| `/recap` | Send this month's recap and PNG chart now |
| `/recap 2026-10` | Send a historical month's recap |
| `/help` | Show command help |

`/add` saves immediately and returns the entry ID and monthly total. Correct mistakes using the dashboard's edit icon. Amounts must be positive with at most two decimal places. Dates use the configured local timezone; future expenses are rejected. Messages delayed by a restart use the original Telegram message date unless you provide an explicit date.

Categories: `food` (including groceries), `transport`, `travel`, `shopping`, `subscription`, `personal_care`, `health`, `fitness`, `entertainment`, `social` (including dates), `gifts` (including donations), `home` (including utilities), `education` (learning/career), `insurance`, and `other`. Your gym membership is initially in subscriptions; one-off sports purchases can use fitness.

Automatic subscription entries already count towards the total and budget. **Do not submit those same charges again through `/add`.** Pausing a subscription stops future unrecorded charges and retains history. Days 29–31 are clamped to the last day of shorter months. Deleting an automatic entry removes it from spending and prevents it from being recreated for that month.

## Monthly recap behavior

Keep the Telegram worker running with internet access. It checks the local calendar after each polling batch. At 23:59 on the final calendar day, it sends one photo containing a chart and a caption with totals, budget status, and category amounts. The chart is generated locally; no third-party chart service receives your ledger.

The recap is a snapshot at dispatch time. Expenses entered afterwards, including later in that final minute, are visible in the dashboard and an on-demand `/recap`; the automatic recap is not reissued. The worker drains queued Telegram messages before sending automatic recaps. Recap delivery state and polling offsets survive restarts. A missed completed-month recap is retried after downtime. Rarely, a crash after Telegram accepts a photo but before the local delivery marker commits can cause a duplicate recap; Telegram provides no general exactly-once send key. Recorded expense submissions and monthly subscription charges have durable deduplication keys.

This tracks **recorded expenditure**, not bank account balances. Automatic charges represent your configured billing schedule, not confirmation from your bank.

## Configuration

| Variable | Default / purpose |
| --- | --- |
| `DASHBOARD_PASSWORD` | Required; at least 12 characters. Changing it invalidates existing dashboard sessions. |
| `PORT` | `8081` |
| `BIND_ADDRESS` | `0.0.0.0`; use the VM's LAN IP or `127.0.0.1` as appropriate |
| `TZ` | `Asia/Singapore`; used by both services for dates, recurring charges, and recaps |
| `COOKIE_SECURE` | `false` for trusted LAN HTTP; set `true` when using HTTPS |
| `TELEGRAM_BOT_TOKEN` | New bot's BotFather token |
| `TELEGRAM_ALLOWED_USER_ID` | Numeric personal Telegram account ID |
| `COMPOSE_PROFILES` | Set `telegram` to run the optional worker |
| `DATA_DIR` | Local development only; Docker uses `/data` |

For external access, use your VPN or an HTTPS reverse proxy. If using a reverse proxy, preserve `Host`, forward the correct scheme, and configure Uvicorn's trusted proxy addresses to match only that proxy. Do not publish plain HTTP credentials to the internet. The dashboard is a single-owner journal with a 30-day HTTP-only session cookie and rate-limited login. `.env` and runtime data are ignored by Git and excluded from Docker images.

## Local development and verification

Python 3.12+; no Node build or CDN is required for the dashboard.

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

[Feature ideas awaiting your review](docs/suggestions.md) are deliberately not implemented.

Implementation references: [Telegram Bot API](https://core.telegram.org/bots/api), [FastAPI security](https://fastapi.tiangolo.com/tutorial/security/), and [SQLite WAL](https://sqlite.org/wal.html).
