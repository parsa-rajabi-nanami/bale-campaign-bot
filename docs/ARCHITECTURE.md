# Architecture

```mermaid
flowchart LR
  Bale[Bale Bot API] --> Client[bale client]
  Client --> Dispatcher[update dispatcher]
  Dispatcher --> Router[router and handlers]
  Router --> Services[services]
  Services --> Repositories[repositories]
  Repositories --> DB[(SQLite or PostgreSQL)]
  Scheduler[scheduler] --> Services
  Sender[sender worker] --> Client
  Sender --> Repositories
  Config[environment and business YAML] --> Services
```

The Bale client is the only layer that makes HTTP requests. It maps response envelopes into typed models and exposes safe errors. Handlers perform authorization and input parsing; services own business rules; repositories own database queries. Services do not import handlers.

The schema is single-tenant. Subscriber chat IDs are the messaging address; phone numbers are optional matching metadata and do not create a subscription. Subscriber consent and opt-out state are persisted. Campaign recipients are intended to be materialized as a durable snapshot before sending.

The admin campaign wizard stores its progress in `conversation_states`, so an interrupted wizard resumes after process restart. It saves the source chat/message pair and previews with `copyMessage`; it does not reconstruct or reformat the payload. Audience filters include every selected tag and exclude any selected tag, always against active subscribers. The recipient count shown in the draft is a preview; the sender must materialize a fresh recipient snapshot when the campaign starts. Campaign CTA buttons are stored separately and are sent as an explicit follow-up after the copied payload because `copyMessage` has no documented keyboard override.

Time-sensitive operations accept an explicit timestamp or clock. Database timestamps use UTC; business-local scheduling and display use the configured IANA timezone, defaulting to `Asia/Tehran`.

## Delivery guarantee

The database stores each campaign-recipient pair uniquely and tracks a recipient through `pending`, `sending`, and a terminal state. Recipient claims and the outcomes for each bounded batch are persisted transactionally. A process crash or ambiguous network timeout after Bale accepted a copy but before the batch result committed can result in duplicates for already delivered members of that claimed batch; the maximum affected group is the configured internal batch size (100 maximum, 10 by default with the default concurrency). Delivery is at-least-once across that failure window; Bale does not expose an idempotency key for `copyMessage` in the API contract used here.

## Durable state

The schema includes subscribers and tags, admins, conversation state, processed update IDs, campaign and recipient records, application state, and a scheduler lease. Migrations are applied with `python -m bale_campaign_bot.cli migrate` and should be backed up before production upgrades.

The sender holds the scheduler lease while it activates due campaigns, recovers old `sending` rows, and sends work. It materializes recipients with an `INSERT … SELECT` query, so a 10,000-member audience is not loaded into memory at once. Active status is checked again before a recipient copy; a later `/stop` therefore excludes pending recipients. A separate follow-up failure never causes the original copied payload to be resent.
