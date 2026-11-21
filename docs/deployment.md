# Proxmox deployment and automatic GitHub updates

Repository: https://github.com/e-mny/budget_tracker_telegram

Pushes to `main` run the application and deployment tests, build a Linux amd64 image, verify its isolation/private-data exclusions, and publish it to **`ghcr.io/e-mny/budget_tracker_telegram`**. Both dashboard and Telegram use that same image. A **Penny** systemd timer on the Docker host checks for new images every five minutes and recreates changed services with the existing expense volume. It does not modify the ACL tracker's timer or containers. No inbound GitHub webhook or SSH deployment port is required.

## One-time setup on your Linux Docker VM

Run inside the VM/LXC where Docker is installed, not on the Proxmox hypervisor. Requirements: Docker Engine, Compose v2.20+, Bash, Python 3, `flock`, and systemd. Use a bridged adapter and reserved LAN IP for desktop access.

Wait for the first **Test and publish Penny** workflow on GitHub to succeed. The workflow uses GitHub's `GITHUB_TOKEN` with package write permission; repository/org policies must allow Actions and package publishing.

Clone the repository (or fast-forward an existing clean checkout):

```sh
git clone https://github.com/e-mny/budget_tracker_telegram.git
cd budget_tracker_telegram
# Existing checkout: git pull --ff-only
```

For a fresh installation only:

```sh
cp .env.example .env
chmod 600 .env
```

For an existing Penny installation, **retain its `.env`** and privately back it up. Configure the dashboard password (12+ characters), Telegram token, personal user ID, and `COMPOSE_PROFILES=telegram` if using the bot. Merge these deployment values into that same `.env`:

```dotenv
COMPOSE_PROJECT_NAME=money-tracker
IMAGE_REPOSITORY=ghcr.io/e-mny/budget_tracker_telegram
IMAGE_TAG=latest
MONEY_DATA_VOLUME=money-tracker_money_data
```

Keep the **existing project name and exact volume name** if they differ. Changing project names can start a second Telegram worker with the same token. To inspect an existing deployment:

```sh
docker compose ls
docker volume ls
docker inspect --format '{{range .Mounts}}{{println .Destination .Name}}{{end}}' DASHBOARD_CONTAINER_ID
```

The existing `/data` mount contains the ledger, receipt drafts, sessions, budgets, goals and notification history. Production Compose uses an **external volume** and refuses to silently create a replacement. The updater verifies it matches any existing container in this project.

**For a genuinely new installation only**, create the selected volume once:

```sh
docker volume create money-tracker_money_data
```

New GHCR packages may be private. For a private image, log in as root (the updater's user) with a GitHub classic personal access token with `read:packages` and access to the package. Enter the token at the password prompt, never as a command argument or in source:

```sh
sudo docker login ghcr.io --username e-mny
```

Public images allow anonymous pulls. GitHub package visibility is managed separately from the repository; no visibility change is made by the workflow.

Run the first image deployment, then enable the timer:

```sh
sudo env PENNY_DEPLOY_DIR="$PWD" bash deploy/update.sh
sudo docker compose -f compose.production.yaml ps
sudo bash deploy/install-auto-update.sh "$PWD"
sudo systemctl list-timers penny-update.timer
```

Open **http://VM-LAN-IP:8081**. Configure subscription billing days and your budget. Press Start in the new bot's private chat. For group use, follow the README: discover the negative chat ID with `/whoami@YourBotUsername`, then configure `TELEGRAM_ALLOWED_GROUP_ID` and `TELEGRAM_NOTIFICATION_CHAT=group`. An optional `TELEGRAM_NOTIFICATION_THREAD_ID` selects the automatic-message topic. Keep only the Proxmox worker running with the live bot token.

## Normal update flow

Commit reviewed changes and push them to `main`. GitHub tests, builds, and publishes the release. The timer pulls it at the next check (about five minutes after publishing; build time is additional). No source rebuild is needed on Proxmox.

The updater serializes runs with a lock, checks volumes before pulling, verifies matching 40-character image revision labels, and waits for dashboard health. Failed pulls or invalid revisions leave running containers alone. Configuration/Compose/updater changes still require a deliberate server `git pull --ff-only` and installer rerun when applicable. The timer does not pull Git source or overwrite `.env`.

Monitor or trigger updates:

```sh
sudo journalctl -u penny-update.service -n 100 --no-pager
sudo systemctl start penny-update.service
sudo docker compose -f compose.production.yaml logs --tail=50 telegram
```

Budget alerts run after each polling batch; subscription reminders run after 09:00 local time within the configured lead window. Both depend on the bot worker and outbound Telegram access. OCR is installed in the image and runs locally.

## Rollback

Disable scheduled checks while reviewing a rollback:

```sh
sudo systemctl disable --now penny-update.timer
```

The updater records source revisions in `.deploy-state/current-release` and `.deploy-state/previous-release`. Set `.env` to `IMAGE_TAG=sha-FULL_PREVIOUS_COMMIT`, verify that release is compatible with the current database, then run `sudo systemctl start penny-update.service`. A first deployment may have no previous labeled revision. An image rollback does not reverse database migrations. Keep a private backup before updates. Leave the tag pinned until ready to resume `latest`.

## Backup and restore

Stop both writers, then back up the complete volume:

```sh
sudo docker compose -f compose.production.yaml --profile telegram stop
sudo docker compose -f compose.production.yaml run --rm --no-deps -T --entrypoint tar dashboard -czf - -C /data . > money-backup.tar.gz
sudo docker compose -f compose.production.yaml up -d
```

Back up `.env` separately through secure secret storage. The archive contains financial data, review drafts, settings and sessions; restrict access and keep an off-server copy. CSV is an expense transfer format, not a full backup.

Restore into an **empty** deployment volume with the same configuration:

```sh
sudo docker compose -f compose.production.yaml --profile telegram stop
sudo docker compose -f compose.production.yaml run --rm --no-deps -T --entrypoint tar dashboard --no-same-owner -xzf - -C /data < money-backup.tar.gz
sudo docker compose -f compose.production.yaml up -d
```

Do not merge unrelated database/WAL files. The image uses UID 10001 and Docker initializes a new named volume with that ownership; a host bind mount must be provisioned for UID 10001. Do not use NFS/SMB for this SQLite file. Source-build `docker compose down -v` deletes its managed data volume; preserve the volume during updates.

## Source builds

For local development or manual deployment, ordinary `compose.yaml` still supports `docker compose up -d --build`, with its own persistent `money_data` volume. Keep the same project and volume names when switching to production images. Use `compose.production.yaml` consistently once the timer is installed.

## Troubleshooting

- Missing volume: confirm the existing container's `/data` mount. Never create a new volume merely to bypass the check on an existing deployment.
- Unauthorized image pull: check root's GHCR login, package access and image visibility.
- Missing manifest: wait for the first publish to finish and verify the image prefix/tag.
- Bot has no replies: confirm the new token, personal numeric user ID, Telegram profile, and Start in private chat. Only the configured owner is allowed; groups also require `TELEGRAM_ALLOWED_GROUP_ID`. Use `/whoami@YourBotUsername` to discover the group/topic IDs and address group commands to this bot. Receipt photos should be replies to a message from this bot.
- Group changes not applied: first run `git pull --ff-only` to obtain the group-aware Compose configuration, then recreate the Telegram worker after updating `.env`; GitHub image updates do not set your group ID. Keep privacy mode enabled and use addressed commands. If a group is upgraded to a supergroup, discover and configure its new ID.
- Telegram polling conflict: stop the other worker using that same token.
- Notifications missing: check worker logs, notification settings, timezone and internet access. Alerts need a configured budget; renewal reminders use the configured schedule, not bank data.
- Wrong recurring charge: edit the recorded expense and the subscription's future settings separately.
- Password/login errors: check the 12-character minimum, HTTPS cookie setting, and reverse-proxy Host/scheme configuration.
- Receipt OCR error: try a sharp, upright English receipt, or enter the expense manually. Extraction never saves automatically.

References: [GitHub publishing](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images), [GHCR access](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry), [local Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).
