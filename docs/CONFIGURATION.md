# Configuration

The bot is single-tenant: one deployment owns one Bale bot and one business YAML file. Keep secrets in environment variables or `.env`; do not commit `.env`.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BALE_BOT_TOKEN` | unset | Bot token. Required for polling and Bale API operations; read as a secret. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/bale_campaign_bot.db` | Async SQLite or PostgreSQL URL. `sqlite:///…` and `postgresql://…` are normalized to their async drivers. |
| `ADMIN_IDS` | empty | Comma-separated positive Bale user IDs for additional administrators. The owner is established through the one-time claim flow. |
| `BUSINESS_CONFIG_PATH` | `config/business.yaml` | Business YAML location. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `LOG_JSON` | `true` | Emit structured JSON logs when true. |
| `HTTPS_PROXY` | unset | Optional outbound proxy passed to the Bale HTTP client. |
| `SEND_RATE_PER_SECOND` | `10` | Conservative campaign send-rate starting point; Bale does not publish a stable limit. |
| `SEND_CONCURRENCY` | `5` | Maximum concurrent campaign sends. |
| `TIMEZONE` | `Asia/Tehran` | IANA timezone used for operator-facing time. Timestamps are stored in UTC. |
| `SETUP_CODE_TTL_SECONDS` | `3600` | Lifetime of the private-chat owner claim code; minimum 60 seconds. |
| `UPDATE_MODE` | `polling` | `polling` works without public ingress. Set to `webhook` only behind HTTPS. |
| `WEBHOOK_PUBLIC_URL` | unset | Public HTTPS base URL, without a query string; webhook mode requires it. Bale supports ports 443 and 88. |
| `WEBHOOK_PATH_SECRET` | unset | Random URL-safe secret with at least 32 characters. It is appended as the webhook path; do not publish it. |
| `WEBHOOK_BIND_HOST` | `0.0.0.0` | Local ASGI bind host. Keep the port private behind a TLS reverse proxy. |
| `WEBHOOK_BIND_PORT` | `8080` | Local ASGI port; the public reverse proxy must expose Bale-supported HTTPS port 443 or 88. |
| `WEBHOOK_MAX_BODY_BYTES` | `1000000` | Maximum webhook request body; accepted range is 1 KiB–10 MB. |

All settings are validated on startup. `ADMIN_IDS` accepts values such as `12345,67890`, not JSON arrays. Database URLs must target SQLite or PostgreSQL; PostgreSQL credentials should be URL-encoded.

## Business YAML

Start with [`config/business.example.yaml`](../config/business.example.yaml), copy it to `config/business.yaml`, then edit:

- `business.name`, `short_description`, `welcome_message`, and `about_text` are required and length-checked.
- `business.contact` contains optional phone, HTTP(S) website, address, and social URLs.
- `business.phone_linking_enabled` enables the explicit Bale contact-sharing button. Only contacts whose Bale user ID matches the sender are accepted.
- `working_hours` and `support_contact` are optional display values.
- Each `default_menu` button must have a label and exactly one action: `url` or `callback`. Callback data is limited to 64 UTF-8 bytes as required by Bale.
- `consent_text` is shown when a user starts or restarts the bot, and `consent_version` is stored with that opt-in. Change the version whenever the consent wording changes.
- `opt_out_confirmation_text` is sent after `/stop`. `footer_text` is sent separately only when `footer_enabled` is true.
- `timezone` must be an installed IANA timezone. `locale` is `fa` or `en`; Persian is right-to-left.
- `campaign_defaults` controls the default rate, concurrency, and optional quiet-hours window.

## Campaign delivery

The admin wizard previews by copying the original message, accepts an all-active or tag-filtered audience, and records scheduled campaigns in UTC after interpreting the entered time in the business timezone. Quiet-hours selections move forward to the next allowed time. CTA buttons and the optional business footer are sent as separate follow-up messages because Bale does not document a `copyMessage` keyboard override.

The sender uses a database lease so only one process schedules and sends campaigns at a time. It claims bounded recipient batches, adapts its global rate after rate-limit responses, and retries transient network/5xx failures up to five recipient attempts. Choose a low starting `SEND_RATE_PER_SECOND`; Bale publishes no stable throughput guarantee. Pause, resume, and cancel campaigns in the private admin chat with `/pause_ID`, `/resume_ID`, and `/cancel_ID` using the ID shown by «کمپین‌ها».

Run `python -m bale_campaign_bot.cli doctor` after editing. Validation errors include an English field path and Persian/English error heading.

## Contact matching and export

`python -m bale_campaign_bot.cli import-contacts contacts.csv` accepts a UTF-8 CSV/TXT file up to 1 MB and 50,000 rows. It matches Iranian mobile numbers against numbers subscribers voluntarily linked after `/start`. It reports matched, unmatched, and invalid counts; repeated valid numbers are counted once. Unmatched numbers cannot be messaged; this command never creates subscribers or sends messages. The private admin `/import` flow accepts the same file types and limits.

`python -m bale_campaign_bot.cli export subscribers.csv` exports active subscriber records with file mode `0600`. Add `--include-inactive` to include opted-out history. Store exports as sensitive personal data and delete them when no longer needed.

## Onboarding

`python -m bale_campaign_bot.cli init` validates the supplied token with `getMe`, prompts for the business values, and writes `.env` and `config/business.yaml` with restrictive mode `0600`. It asks before replacing either existing file. Admin IDs are not hard-coded: after first startup, the first person to send the generated one-time claim code in a private chat becomes owner.
