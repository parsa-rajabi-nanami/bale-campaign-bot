# Bale API notes and assumptions

The official Bale Bot API documentation at <https://docs.bale.ai/> is the reference. This project implements only a small hand-written HTTP client; it does not depend on Telegram libraries or infer undocumented parity.

## Documented behavior used

- API methods use `https://tapi.bale.ai/bot<TOKEN>/<METHOD>`; file downloads use `https://tapi.bale.ai/file/bot<TOKEN>/<file_path>`.
- API methods are case-insensitive. GET/POST and query, form, JSON (except uploads), and multipart upload requests are documented.
- Responses have an `ok` boolean and may include `result`, `description`, `error_code`, and `parameters`.
- Long polling uses `getUpdates`; `limit` is 1–100, and accepted offsets acknowledge prior updates. Bale retains only the last 2000 updates for 24 hours.
- Documented update variants include `message`, `edited_message`, and `callback_query`.
- Text sent with `sendMessage` is limited to 1–4096 characters; media captions have a 1024-character limit.
- Bale markup has no documented `parse_mode`; broadcast payloads therefore use `copyMessage` to preserve the source message and entities.
- `copyMessage` returns a `MessageId`; the documented method has no reply-markup override. Campaign CTA buttons must be a separate follow-up unless live verification establishes support.
- This project keeps campaign CTA buttons in the database and sends them as a separate `sendMessage` follow-up after the copied campaign payload. That follow-up behavior and whether all supported destination chats accept these button keyboards still require live verification; failure must not cause the original campaign copy to be resent.
- `editMessageText` does not work for messages with an inline keyboard. Broadcast progress messages must have no keyboard; controls are separate.
- `deleteMessage` is documented only for messages less than 48 hours old.
- A bot can message only a chat that has started it. Phone-number import can identify linked opt-in users but must never subscribe or directly message unmatched numbers.
- Webhooks require HTTPS and support ports 443 and 88; an empty URL disables the webhook.

## Defensive assumptions requiring live verification

These behaviors are not specified sufficiently to rely on without defensive handling. The client must tolerate failure and must not let any one failure crash the process.

1. **Rate limiting:** Rate limits and the exact 429 response shape are not documented. Honor a numeric retry hint in `parameters` if present, or `Retry-After`; otherwise use bounded exponential backoff with jitter. Adapt the global send rate conservatively. When HTTP 429 has no usable Bale envelope, the client preserves the header retry hint in its typed HTTP error for the sender.
2. **Retry hint keys and units:** `parameters` is documented generically, but exact field names, numeric types, and units are not. Parse known candidates defensively and ignore malformed values.
3. **Transient failures:** Whether Bale uses particular HTTP status codes for internal errors, overload, or temporary unavailability is not documented. Retry network errors and 5xx responses with a bounded policy; do not infer success from malformed envelopes.
4. **Permanent recipient failures:** Exact codes/descriptions for blocked bots, forbidden chats, and missing chats are not stable/documented as a machine-readable contract. Classify using conservative status/code/text patterns, preserve the raw safe error class, and manually validate against a live bot.
5. **`answerCallbackQuery`:** The project request identifies this method as undocumented and requires defensive isolation. The current Bale docs page appears to list it; availability and exact parameters/behavior must still be checked on the deployed API version. Failure is non-fatal.
6. **Command registration and membership:** `setMyCommands` and `getChatMember` are treated as undocumented for this project. Calls, if added, must be optional and failure-tolerant. Authorization must use the configured user-ID allowlist, never a membership lookup.
7. **Rate limits:** No safe throughput or concurrency contract is documented. The configured default is an operator-controlled starting point, not a Bale guarantee.
8. **Webhook validation:** The documented `setWebhook` parameters do not describe a secret header. Use an unguessable secret path and constant-time path comparison; confirm actual delivery behavior live.
9. **`copyMessage` media/markup coverage:** The request states the method preserves supported message content but does not document every media type, protection restriction, or behavior around keyboards/captions. Verify each campaign content type manually. Do not attach CTA markup to the copy request.
10. **Contact sharing:** Bale's `request_contact` behavior and returned contact identity fields should be verified with a real client. Accept only a contact whose `user_id` equals the sending user.
11. **Phone normalization:** The application normalizes Iranian mobile formats (`09…`, `+98…`, `0098…`) for matching. This is application behavior, not a Bale guarantee.
12. **Undocumented update delivery details:** Long-poll timeout ceilings, retry cadence, duplicate delivery edge cases, and webhook retry/acknowledgment rules are not treated as stable guarantees. Persist update IDs and make processing idempotent.
13. **File limits:** This project must enforce its own strict upload size and CSV row limits. Any Bale-side download/upload size limits must be verified separately.

## Manual live-bot checklist

Before a production rollout, verify `getMe`, `getUpdates` offset/timeout behavior, private `/start` and `/stop`, each campaign media type via `copyMessage`, forbidden/blocked recipient errors, actual 429 hints, callback acknowledgments, contact sharing, webhook delivery/retries, and the operator's chosen send rate. Do not test broadcast behavior against real subscribers without explicit consent.
