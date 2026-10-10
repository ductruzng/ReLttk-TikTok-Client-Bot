# Phase 2 — Ledger V2

Scope: SQLite migration, durable request records, idempotency and intentional
repeat. No live TikTok verification, runtime database migration, session access,
deployment or Git operation was performed during implementation.

## Schema and migration

The existing ledger file is the only Safety Layer database. `PRAGMA user_version`
is 2 after migration; `schema_migrations` records the version, time and backup path.
Opening a writable ledger migrates versions 0/1 automatically. Read-only status
does not migrate. Unknown schema versions are rejected by the writable path.

| Table | Purpose |
| --- | --- |
| `daily_ledger` | Original rows retained verbatim; INSERT/UPDATE/DELETE blocked after migration |
| `send_requests` | Request ID, account, conversation, key, canonical payload, client UUID, lifecycle, dates, echo ID and transmitted marker |
| `request_events` | Append-only creation, transmission, terminal outcome, import and recovery events |
| `crossover_guards` | Preserved conservative midnight guards, separate from transmitted quota |
| `schema_migrations` | Applied schema version and pre-migration backup location |

`UNIQUE(canonical_uid, idempotency_key)` is independent of daily/repeat policy.
There is no account/conversation/day uniqueness constraint. A partial unique index
permits only one `pending` transmission in the database. New requests also have
unique per-account client message IDs. Terminal requests and request identities
cannot be overwritten; requests and events cannot be deleted through normal SQL.

Migration holds `BEGIN IMMEDIATE` while a separate read connection uses SQLite's
Backup API to write a unique `*.pre-v2-<uuid>.db`. The source reader includes committed
WAL pages. No raw database/WAL file copying or forced checkpoint is used. The backup
passes `integrity_check` before migration proceeds. DDL, import, version and audit
record commit in one transaction. On failure they roll back; the backup is retained.
Re-running migration after success does not re-import or make another backup.
Fresh empty databases need no legacy backup.

Legacy mapping:

- `confirmed` → `confirmed`, consumes one transmitted slot on its original date.
- `pending` / `failed_unknown` → `failed_unknown`, conservatively consumes one slot
  and blocks the conversation across dates. Legacy lacks a reliable transmission boundary.
- `blocked_crossover` → a guard plus a non-transmitted import record, not an extra send.
- Unrecognized legacy states → `failed_unknown` conservatively.

All original fields remain in `daily_ledger` and an import event. Missing legacy
message content is not invented. Legacy rows receive isolated import keys; their
unknown original payload cannot be used as an idempotent replay of a new message.

## Request lifecycle and Settings

`queued` → `pending` → `confirmed` or `failed_unknown`.
Packet-building errors instead yield `failed_pretransmit` with transmitted=0.
The queue is durable storage; this phase does not add a background queue consumer
or automatic retries. An explicit invocation may resume an existing queued request.
Terminal requests are returned/skipped without another transmission.

Same account/key and canonical payload returns the existing request. A different
payload or conversation under that key raises `IdempotencyConflict`. Settings are
not part of message identity. Quota is checked atomically at transmission, not when
a queued request is created. Quota denial leaves that request queued and shows the
blocking setting/usage; a new key does not bypass it.

Settings live in the existing plan JSON:

```json
{
  "allow_repeat_same_day": false,
  "max_sends_per_conversation_per_day": 1
}
```

Both defaults are restrictive. Repeat requires a new key, `allow_repeat_same_day=true`
and spare daily quota. Usage counts transmission markers, including UNKNOWN, not
just SUCCESS. A definite pre-transmission failure consumes no quota. These are local
policy settings, not TikTok-approved thresholds.

CLI accepts `--idempotency-key`. Without one, the one-shot path retains a stable
daily key per conversation. Interactive CLI/TUI manual actions generate a new key;
the existing TUI scheduled callback reuses its scheduled occurrence key. No new
scheduler was added or enabled. All three entry points call `services.send_plan`.
The existing skip-repeat switch now saves the repeat policy; it never deletes rows.
The CLI settings menu can change the daily limit; TUI users edit that field in the
plan JSON (no new interface added).

SQLite `BEGIN IMMEDIATE`, constraints and a committed transmission marker prevent
two contenders from transmitting the same request. The run lock serializes the
one-shot workflow. Recovery requires that held database run lock, changes abandoned
`pending` to UNKNOWN, and appends an event. Queued/uncommitted work consumes no
transmitted quota. A crash after marker commit but before `ws.send` is conservatively
UNKNOWN, even if nothing left the device. This cannot guarantee exactly-once
delivery across a network failure; it prevents automatic resending of ambiguous work.

UNKNOWN never expires at midnight or by increasing Settings. Owner resolution and
replacement linking/reasons through CLI are deferred; V2 deliberately offers no
bypass or reset command yet. A later resolution must append events, retain the old
UNKNOWN and quota, and subject a new replacement request to normal limits.

## Validation and rollback

Offline regression command on the prepared Windows environment:

```powershell
.\work\verify-venv\Scripts\python.exe -B tests\run_offline.py
```

The runner blocks external networking and access to known runtime data paths.
Fixtures cover WAL backup, unchanged legacy rows, repeated/concurrent migration,
injected migration failure and rollback, restored backup contents, process contention,
unique constraints, atomic event/state changes, real child-process exits around the
transmission marker, fake-WebSocket repeat/replay/conflict and pre-transmit failure.
SQL/source checks reject destructive repeat branches in the CLI/TUI/service/ledger paths.

Rollback must be offline with all clients stopped. Restore the Backup API snapshot
to a separate database and validate it before selecting the matching old application.
Do not overwrite an active database or mix its WAL/SHM with another database file.
Preserve the V2 database with another Backup API snapshot first. If any V2 requests
have started transmission, reverting to the pre-migration snapshot loses their
quota/dedupe knowledge: do not resume sending until that history has been reconciled.
There is no automatic destructive downgrade. Fixture restore is verified; restoration
of a real database has not been authorized or attempted.

Still outside this phase: rate limiter, circuit breaker, message rotation, scheduler
development, complete interactive/noninteractive confirmation policy, Owner UNKNOWN
resolution CLI, management of every direct legacy send API, and cross-device testing.
The legacy client cache is not part of this ledger migration. Correlated server echo
confirms server acceptance only, not recipient delivery/read or TikTok streak credit.
TikTok endpoints remain unofficial internal APIs.
