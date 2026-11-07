# Deployment

## Docker Compose (SQLite)

Run `python -m bale_campaign_bot.cli init` on the host first. It creates `.env` and `config/business.yaml`; do not commit either file. Ensure Docker can write the data directory as UID 10001:

```sh
mkdir -p data
sudo chown 10001:10001 data
docker compose up --build -d
docker compose logs -f bot
```

The image has separate build and runtime stages and runs as the unprivileged `bot` user. Business configuration is mounted read-only. SQLite data lives in `./data`. The service restarts on failure and allows two minutes for graceful shutdown.

## Local process

Install locked dependencies with `uv sync --locked --all-groups`, run `uv run python -m bale_campaign_bot.cli init`, then `uv run python -m bale_campaign_bot.main`. The process applies migrations at startup. Run `uv run python -m bale_campaign_bot.cli doctor` after startup or configuration changes.

## PostgreSQL profile

Set a strong `POSTGRES_PASSWORD` in the private `.env` (the postgres profile will refuse an empty password) and set:

```dotenv
DATABASE_URL=postgresql+asyncpg://bale_campaign_bot:URL_ENCODED_PASSWORD@postgres:5432/bale_campaign_bot
```

Start PostgreSQL first and then the bot so the database is ready before the bot applies migrations:

```sh
docker compose --profile postgres up -d postgres
docker compose up --build -d bot
```

URL-encode reserved characters in passwords. PostgreSQL uses the named `postgres_data` volume. Back it up using PostgreSQL's supported backup tooling before upgrades and on a regular schedule. The bot's delivery lease is database-backed, but only one process should poll or serve updates for a bot token.

## Webhook mode

Polling is the default and needs no inbound port. For webhook mode:

1. Put the bot behind a TLS reverse proxy that accepts HTTPS on Bale-supported public port 443 or 88.
2. Choose a long random URL-safe path secret (at least 32 characters) and configure:

   ```dotenv
   UPDATE_MODE=webhook
   WEBHOOK_PUBLIC_URL=https://bot.example.com/bale
   WEBHOOK_PATH_SECRET=replace-with-a-random-URL-safe-secret-of-32-or-more-characters
   WEBHOOK_BIND_HOST=0.0.0.0
   WEBHOOK_BIND_PORT=8080
   ```

3. Forward `/bale/<secret>` to the container's configured webhook port. The app registers the final URL with Bale, validates the path in constant time, limits request bodies, and disables HTTP access logs so the secret path is not recorded. The Docker Compose port binds to host loopback; expose it only through the trusted reverse proxy.
4. Run `doctor` and manually verify delivery, response status, proxy path handling, and retries with a live bot. Bale's supplied API contract does not document a secret header or detailed webhook acknowledgment/retry behavior.

Switching back to `UPDATE_MODE=polling` clears the configured webhook before long polling starts. Do not run more than one update intake process for the same bot token.

## Secrets, logs, and backups

- Keep `.env`, the webhook path secret, backups, and subscriber exports private. `.env` and CLI exports are written with mode `0600`.
- Never publish setup-code logs while a claim code is still valid. Claim codes are single-use and expire.
- Logs contain event names, ids, counts, and error classes, not tokens or campaign body text. Do not enable reverse-proxy access logging for webhook paths.
- Back up the database before migrations. Test restore procedures; a database backup does not include `.env` or `config/business.yaml`.
- Subscriber phone numbers and exports are personal data. Restrict access and retention to what your business needs.

## Stop and update

`docker compose stop` sends SIGTERM; the sender stops claiming batches and waits for in-flight sends to persist. On update, pull the selected source revision, rebuild with `docker compose up --build -d`, and inspect logs and `doctor`. Take a database backup before changing revisions.
