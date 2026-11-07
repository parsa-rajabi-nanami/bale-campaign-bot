# Repository conventions

## Product and API constraints

- This is a single-tenant, self-hosted Bale campaign bot. Subscribers must opt in by starting the bot; imported phone numbers never create subscribers and cannot be messaged directly.
- Follow the Bale API documentation and `docs/BALE_API_NOTES.md`. Do not assume Telegram compatibility. All HTTP details and defensive handling for undocumented behavior belong in `bale/`.
- Never log tokens, secrets, or full campaign message bodies. Keep credentials in environment variables and never commit `.env` files.
- Admin actions require both an allowlisted sender ID and a private chat. Treat callback data, user input, imported files, and API error descriptions as untrusted.

## Architecture

- Keep handlers thin: handlers parse/authorize requests and call services; business rules live in services; services must not import handlers.
- The Bale client is the only module that performs Bale HTTP requests.
- Keep database access behind repositories and use SQLAlchemy 2.x async APIs.
- Persist update offsets, FSM state, campaign state, and recipient delivery state so restarts do not silently lose work.
- Inject clocks into time-sensitive services. Store timestamps in UTC and display in the configured timezone (Asia/Tehran by default).
- Use typed models and type hints throughout. Keep public interfaces small and failures explicit.

## Quality and workflow

- Python 3.12, `uv`, Ruff, mypy strict, pytest, pytest-asyncio, and respx are the supported toolchain.
- Before moving to the next requested phase, run that phase's relevant tests, lint, and type checks; fix failures before continuing.
- Add tests for behavior and failure paths. Keep tests deterministic, offline, and free of real Bale credentials.
- Keep migrations additive and reviewable. Document assumptions and operational limitations in `docs/`.
- Use clear, focused commits at phase boundaries when requested. Do not push, tag, merge, or release without explicit authorization.
