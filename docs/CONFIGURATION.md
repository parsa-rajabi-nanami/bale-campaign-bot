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

All settings are validated on startup. `ADMIN_IDS` accepts values such as `12345,67890`, not JSON arrays. Database URLs must target SQLite or PostgreSQL; PostgreSQL credentials should be URL-encoded.

## Business YAML

Start with [`config/business.example.yaml`](../config/business.example.yaml), copy it to `config/business.yaml`, then edit:

- `business.name`, `short_description`, `welcome_message`, and `about_text` are required and length-checked.
- `business.contact` contains optional phone, HTTP(S) website, address, and social URLs.
- `working_hours` and `support_contact` are optional display values.
- Each `default_menu` button must have a label and exactly one action: `url` or `callback`. Callback data is limited to 64 UTF-8 bytes as required by Bale.
- `consent_text` and `consent_version` describe the opt-in shown to subscribers. Keep the version stable until consent wording changes.
- `opt_out_confirmation_text` is sent after `/stop`. `footer_text` is sent separately only when `footer_enabled` is true.
- `timezone` must be an installed IANA timezone. `locale` is `fa` or `en`; Persian is right-to-left.
- `campaign_defaults` controls the default rate, concurrency, and optional quiet-hours window.

Run `python -m bale_campaign_bot.cli doctor` after editing. Validation errors include an English field path and Persian/English error heading.

## Onboarding

`python -m bale_campaign_bot.cli init` validates the supplied token with `getMe`, prompts for the business values, and writes `.env` and `config/business.yaml` with restrictive mode `0600`. It asks before replacing either existing file. Admin IDs are not hard-coded: after first startup, the first person to send the generated one-time claim code in a private chat becomes owner.
