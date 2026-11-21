# Proxmox deployment

Use Docker inside your existing Linux VM or a dedicated Linux VM on Proxmox. A bridged adapter and reserved LAN IP make desktop access straightforward. No Proxmox-specific API or host modification is required. This project uses its own port and volume; it does not modify the ACL tracker.

## Install and run

Transfer the directory to a location such as `/opt/money_tracker` on the VM:

```sh
cd /opt/money_tracker
cp .env.example .env
chmod 600 .env
```

Edit `.env` locally on the server, choose a unique `DASHBOARD_PASSWORD` of 12+ characters, and optionally configure Telegram as described in the main README.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=50
```

Open `http://VM-LAN-IP:8081`. If 8081 is already occupied, change `PORT` in `.env`. The dashboard has a database-backed health check. The worker depends on dashboard readiness and retries outbound Telegram failures. A missing/invalid bot token is reported through worker logs. The Docker host needs working DNS, accurate system time, and outbound HTTPS to Telegram.

The image runs as UID 10001, with dropped capabilities, a read-only root filesystem, a writable temporary filesystem, and a persistent named data volume. Both services mount that same volume, which contains `ledger.sqlite3` and SQLite WAL files. Run exactly one Telegram worker per bot token and one Compose project per ledger. Do not share this SQLite file over NFS/SMB.

## Update

Replace the application source while retaining `.env` and the deployment's existing volume. Then run:

```sh
docker compose up -d --build
docker compose ps
```

No container registry, GitHub deployment, or automatic updater is configured. This keeps setup independent of accounts you have not provided. The current Compose project name is fixed as `money-tracker`; its default data volume is `money-tracker_money_data`. Keep the same project name for updates.

## Back up

Stop both writers for a consistent backup. These commands explicitly enable the Telegram profile, so they work whether or not it is selected in `.env`:

```sh
docker compose --profile telegram stop
# Store the archive somewhere private, preferably outside this source tree.
docker compose run --rm --no-deps -T --entrypoint tar dashboard -czf - -C /data . > money-backup.tar.gz
docker compose up -d
```

Back up `.env` separately using a secure secret-storage method. The archive contains financial data and session records. Restrict access to it. Keep an off-server copy.

Restore to an empty deployment volume with the same Compose setup:

```sh
docker compose --profile telegram stop
docker compose run --rm --no-deps -T --entrypoint tar dashboard --no-same-owner -xzf - -C /data < money-backup.tar.gz
docker compose up -d
```

Use an empty volume for restore; merging unrelated database/WAL files can corrupt data. The named volume normally assigns ownership to UID 10001 on first use. If you substitute a host bind mount, provision that directory for UID 10001 yourself.

`docker compose down` keeps the volume. **`docker compose down -v` deletes the ledger permanently.**

## Troubleshooting

- Dashboard won't start: check that `DASHBOARD_PASSWORD` is set and at least 12 characters long; inspect `docker compose logs dashboard`.
- No bot replies: confirm the new token, personal numeric user ID, `COMPOSE_PROFILES=telegram`, and that you pressed Start in its private chat. Other users and groups are intentionally ignored.
- Recap missing: keep the worker running, check the timezone and outbound internet. Failed delivery retries automatically; Telegram cannot initiate a private conversation before you start it.
- Bot conflict errors: stop any other deployment polling the same bot token.
- Wrong subscription charge: edit the already recorded transaction, then edit the subscription for subsequent months. Recurring settings do not rewrite history.
- Login loops through a proxy: check HTTPS, `COOKIE_SECURE`, forwarded scheme, Host header, and Uvicorn trusted proxy configuration. Authenticated API writes must be same-origin.
- A budget affects a later month: budgets carry forward by design. Set a replacement for that later month if desired.
