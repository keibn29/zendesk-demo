# Zendesk ↔ Slack Bridge — API

> Full setup guide (Vietnamese, incl. Slack/Zendesk console config) is in the
> [root README](../README.md). This file documents the API package itself.

FastAPI service that bridges Slack and Zendesk:

- **Slack message → Zendesk ticket** (plain-text comment, correlated back to the message).
- **Zendesk ticket status → Slack reaction** on that original message.

## Quick start

```bash
cp .env.example .env   # then fill in the secrets
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

Or with Docker:

```bash
cp .env.example .env
docker compose up --build
```

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET  | `/health` | Liveness probe. Does not call upstream APIs. |
| POST | `/webhooks/slack/events` | Slack Events API receiver (needs `event_callback: message` + `url_verification` subscopes). |
| POST | `/webhooks/zendesk` | Zendesk ticket webhook receiver. |

## Architecture

```
Endpoint (api/)  ->  Service  ->  Repository  ->  Model
                          \-> Client (httpx: Zendesk / Slack)
```

- `app/core/` — settings (`config.py`) and HMAC signature helpers (`security.py`).
- `app/api/` — thin endpoints + signature-verification dependency (`deps.py`).
- `app/services/` — `slack_ticket.py` (ticket creation) and `zendesk_reaction.py` (reactions).
- `app/repositories/` — data access only.
- `app/clients/` — async Zendesk / Slack Web API wrappers.
- `app/db/`, `app/models/` — async SQLAlchemy setup, `create_all` on startup.

## Signature verification

Both webhooks verify the **exact raw request body**; never re-serialize the payload.

- **Slack** — `X-Slack-Signature` = `v0=` + hex(HMAC-SHA256(signing_secret, `v0:` + timestamp + `:` + body)).
  The timestamp is **Unix epoch seconds**.
- **Zendesk** — `X-Zendesk-Webhook-Signature` = base64(HMAC-SHA256(webhook_secret, timestamp_string + body)).
  The timestamp header is **ISO-8601** (usually with a trailing `Z`), e.g. `2024-01-01T12:00:00Z`.

The HMAC always uses the **exact original header string** — it is never reformatted. Freshness is
validated separately by parsing the ISO-8601 value; a naive timestamp (no offset) is rejected because
its age cannot be determined reliably. Both use `hmac.compare_digest` and a 5-minute replay window
(`SIGNATURE_TOLERANCE_SECONDS`), with a bounded forward skew (60s) for clock drift.

`DEV_DISABLE_SIGNATURE_VERIFICATION` exists for local testing only. The app **refuses to start**
with it enabled unless `ENVIRONMENT=development`. There is no permissive production bypass.

`WEBHOOK_SHARED_TOKEN` is optional defense in depth, checked *in addition to* the signature. Leaving
it empty disables the layer; `WEBHOOK_SHARED_TOKEN_HEADER` sets the header name it reads.

## Idempotency and deduplication

Slack requires an ACK within 3 seconds, so ticket creation runs as a FastAPI `BackgroundTask`.

Duplicate suppression does **not** rely on `X-Slack-Retry-Num`, and does **not** key on Slack's
`event_id` (Slack may assign a new id on redelivery). Instead:

1. The `processed_events` table claims the **message coordinates** `team:channel:ts` atomically
   (`INSERT ... ON CONFLICT DO UPDATE ... WHERE`), which also recovers claims abandoned by a crash
   after `PROCESSING_CLAIM_TIMEOUT_SECONDS`.
2. A **UNIQUE constraint** on `(slack_team_id, slack_channel_id, slack_message_ts)` enforces one
   ticket per message at the database level.
3. The `Idempotency-Key` header (derived from the same coordinates) is sent to Zendesk. Zendesk's
   `external_id` is **not** unique, so it is used for correlation only.

Claim states are `processing` / `completed` / `failed`. Known failures are marked `failed` rather
than dropped, so a later delivery retries them instead of being suppressed forever; a retry updates
the same row and increments `attempts`. The mapping row is committed **only after** Zendesk confirms
ticket creation.

Each claim gets an **owner token** (one generation). `complete()`, `fail()` and `release()` only
write when the key, token, and `processing` state all match, so a superseded worker cannot overwrite
newer state or delete a completed claim — including when its own failure recording errors out.

Each upstream call is bounded by a **total** deadline (`asyncio.timeout(PROCESSING_DEADLINE_SECONDS)`),
not just httpx's per-phase timeout, so a slow upstream cannot outlive the lease. The app **refuses to
start** unless `PROCESSING_CLAIM_TIMEOUT_SECONDS >= PROCESSING_DEADLINE_SECONDS +
CLAIM_TIMEOUT_MARGIN_SECONDS`. On deadline the claim is released so a replay can retry immediately.

Because the ACK happens before the background work, a crash in between loses that job and Slack will
not resend it; a durable queue is required for production. A queue alone does **not** close the
upstream-success/local-commit gap — that additionally needs a durable upstream idempotency key or a
reconciliation job.

Zendesk deliveries are deduplicated by `invocation_id` (falling back to `zendesk_event_id`, then `id`).

## Supported Zendesk payload shapes

**1. Modern event webhook** (`zendesk:ticket.status_changed`):

```json
{
  "id": "...",
  "type": "zendesk:ticket.status_changed",
  "zendesk_event_id": "...",
  "detail": { "id": 123, "ticket_id": 123, "status": "solved" }
}
```

**2. Custom trigger payload** (useful if you prefer a webhook target you fully control):

```json
{ "invocation_id": "stable-per-logical-event", "ticket_id": 123, "action": "solved" }
```

`action` may be any status key. `invocation_id` is **required**; a payload without one is rejected
with `400` rather than silently accepted.

It identifies a **logical event** (one status change), not an HTTP delivery. A Zendesk retry of the
same event carrying the same `invocation_id` is suppressed as intended. Generating a fresh
`invocation_id` per retry makes each retry look like a new event.

Malformed-but-authenticated bodies (non-object JSON, non-positive/non-integral `ticket_id`, missing
or wrongly-typed `action`) return `400`, not `200`.

## Reaction mapping

| Status | Reaction (default) | Env override |
| ------ | ------------------ | ------------ |
| new / open | `eyes` | `REACTION_NEW` / `REACTION_OPEN` |
| pending / hold | `hourglass_flowing_sand` | `REACTION_PENDING` / `REACTION_HOLD` |
| solved / closed | `white_check_mark` | `REACTION_SOLVED` / `REACTION_CLOSED` |

Slack `already_reacted` is treated as success. Tickets with no stored Slack mapping are skipped
safely. Requires the `reactions:write` bot scope.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Covers signature vectors for both timestamp formats (Slack epoch, Zendesk ISO-8601: valid/tampered/
replayed/skewed/malformed), settings validation including real `.env` loading, payload normalization
for wrong-typed and non-object bodies, both services (dedup by message coordinates, retryable
failures, unmapped tickets, allowlist), lease ownership across separate sessions, the total
upstream deadline against a genuinely blocked upstream, mapping persistence failures, HTTP client
request shape and error handling, and the webhook endpoints (including that background work calls
the injected fakes).

Isolation guarantees, asserted by `tests/test_netguard.py`:

- every setting is pinned to a test value and `Settings.env_file` is neutralized, so a developer's
  real `.env` is never read;
- `DATABASE_URL` points at a throwaway DB per test;
- outbound `socket.connect` is blocked session-wide by `NetworkAccessDenied`, which derives from
  `BaseException` so the app's `except Exception` recovery paths cannot swallow it;
- a single endpoint fixture patches the app lifespan, so **no real `httpx.AsyncClient` is ever
  constructed** and the default `DATABASE_URL` engine is never touched;
- upstream clients are fakes or `httpx.MockTransport`.

## Schema changes

`create_all` does **not** alter existing tables. If you created the database before the UNIQUE
constraint on message coordinates and the `attempts` column were added, delete the dev SQLite file
(`rm -f data/bridge.sqlite3`) so it is recreated. Use Alembic migrations instead once real data
exists.

## Limitations

- **BackgroundTasks are in-process.** A crash between the ACK and ticket creation loses that job and
  Slack will not resend it. Known failures are recorded as `failed` and can be retried by a replay,
  and abandoned `processing` claims become retryable after the claim timeout, but there is no
  automatic retry. Production needs a durable queue (Celery/RQ + Redis, or an SQS/PubSub producer)
  with the same service functions as the payload body.
- **SQLite + `create_all`, no Alembic.** Fine for a single-instance starter; add migrations and
  Postgres before multi-instance use.
- **Single instance.** `BackgroundTasks` provides no cross-process coordination; the DB claim and
  the UNIQUE constraint are the only concurrency guards.
- **No retry cap.** A Zendesk delivery for a ticket that genuinely has no Slack mapping is recorded
  as `failed` and stays retryable, so repeated deliveries accumulate records. This favours not
  missing a reaction over bounding writes; add an attempt threshold with a durable queue.
- No requester/user mapping sync, no ticket comments relayed back to Slack.