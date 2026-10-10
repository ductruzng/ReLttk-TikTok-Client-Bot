# Phase 3A — Rate Limiting & Daily Limits

Implemented only the approved 3A scope. No real TikTok messages, credentials,
runtime ledger access, Debian deployment or Git operations were used for validation.
Phase 3B (Message Rotation) has not started.

## Settings and behavior

All six settings live in the existing plan JSON. Missing fields use these defaults:

```json
{
  "max_sends_per_account_per_day": 20,
  "max_sends_per_conversation_per_day": 1,
  "allow_repeat_same_day": false,
  "min_send_interval_seconds": 60,
  "max_sends_per_window": 5,
  "window_seconds": 300
}
```

`allow_repeat_same_day` must be a JSON boolean. The other fields must be integers
(not booleans, numeric strings or fractions), at most 2147483647. Counts and window
duration must be positive; interval may be zero to explicitly disable cooldown.
That upper bound is an input representation guard, not a recommended operating limit.
These are local policy thresholds, not TikTok-guaranteed bot-detection avoidance limits.

The CLI settings menu can adjust all fields, including the conversation limit when
intentional repeat is enabled. Existing TUI settings retain the repeat switch;
additional values are configured in JSON without new UI controls. Saving a plan
preserves configured values. CLI/TUI still share the application service.

Daily limits use the claim's date in `Asia/Ho_Chi_Minh`. Cooldown and sliding window
use UTC epoch milliseconds and are account-wide across conversations. The window is
`(now - window_seconds, now]`; an attempt at the left boundary has expired. Cooldown
permits a claim exactly at `last_start + interval`. Neither resets at midnight.

Only `send_requests.transmitted=1` contributes to usage. Confirmed, pending and
UNKNOWN attempts all count. Queued and definitely failed-before-transmission requests
do not count. UNKNOWN also retains its independent conversation block across days.
The existing crossover guard remains conservative; it is not counted as another send.

Idempotency is unchanged: same key/payload returns existing work without claiming
another slot; conflicting payload is rejected. A new key cannot bypass quotas or
`allow_repeat_same_day=false`. Raising Settings does not delete history or resolve UNKNOWN.

## One source of truth and atomic claims

No counter table, in-memory quota cache, token bucket, worker, or external dependency
was added. `send_requests` is the sole source for all quota/window calculations.

`start_transmission` holds `BEGIN IMMEDIATE`, samples one clock instant after acquiring
the writer lock, evaluates every blocker, then writes the transmission timestamp,
date, marker and Settings audit event in the same transaction. A failed transaction
rolls all of these back. The existing unique pending index and process run lock remain.
Two processes sharing the same database cannot both take the last slot. Separate
database copies do not coordinate; do not run the same account with independent ledgers.

After commit, the transport attempts the send. Crash after commit but before the socket
call remains conservatively UNKNOWN on recovery and retains quota. This is not a
guarantee of exactly-once network delivery. Intervals measure committed attempt-start
markers, not the recipient's delivery time. No network operation or sleep occurs in
the claim transaction.

Blocked requests remain queued. There is no automatic sleep/retry: with the default
60-second interval, a batch may send its first target and block subsequent targets.
An explicit later invocation can re-evaluate the same queued request/key. The next
phase must not be assumed to drain this queue automatically.

Block results contain `request_id`, all `blocks` (code, scope, used, limit, detail and
per-rule retry time), and aggregate `retry_at`. CLI and existing TUI logs display the
shared explanation. For finite conditions the aggregate is the latest expiry; UNKNOWN,
pending concurrency or clock skew makes it unknown. This timestamp is only the earliest
time to re-evaluate with unchanged Settings/history, not a reservation or schedule.

Clock rollback before the account's latest persisted transmission returns `CLOCK_SKEW`
without sending. Future markers are retained, never dropped from consideration. As
approved in the plan, the operating system clock must be trustworthy: arbitrary forward
clock jumps, or incorrect wall time across devices, cannot be validated against an
independent trusted clock in this phase. Network waits retain monotonic timeouts.

## Schema V3

`PRAGMA user_version=3`. Existing tables remain, with no counter table:

- `send_requests.transmission_started_at_ms INTEGER`: UTC epoch milliseconds; NULL
  for requests that have not started transmission.
- `request_account_day`: `(canonical_uid, date) WHERE transmitted=1`.
- `request_account_time`: `(canonical_uid, transmission_started_at_ms) WHERE transmitted=1`.
- Marker validation/immutability triggers prevent missing/invalid new transmission
  timestamps or later edits to a committed timestamp/date/attempt time.
- Existing identity, terminal, append-only history and unique-index protections remain.

Migration only adds derived timestamps and schema metadata. Original `attempt_time`,
date, legacy rows, statuses, UNKNOWN, transmitted values and request events are preserved.
Timezone-bearing historical timestamps are converted to UTC; sub-millisecond values
round up conservatively. Missing, malformed, naive timestamps, pre-epoch dates or a
stored daily date inconsistent with Asia/Ho_Chi_Minh block migration. No repairs or
timezone guesses occur. Non-transmitted records may retain missing attempt timestamps.

The terminal trigger is temporarily removed only within the migration transaction
to backfill the new column, then restored before validation/commit. Failure rolls
back both DDL and data, including trigger restoration. Schema validation checks required
objects, the idempotency unique constraint, timestamp types and the migration record;
it does not treat `user_version` alone as proof of a usable ledger.

## Explicit administration; no send-path migration

The send path opens an existing database with SQLite `mode=rw`, verifies schema V3,
and never creates directories/databases, initializes schema or migrates. A read-only
schema gate runs before session loading or TikTok access. Missing/wrong schema reports
a local error. Status and dry-run do not initialize or migrate.

Administration requires an explicit command and exact database path. The commands
below are documentation only; **they have not been executed on runtime data**:

```powershell
# Inspection only; no backup or migration:
python -B main.py ledger-preflight --db "PATH-TO-FIXTURE.db"

# Creates a NEW database; refuses an existing file:
python -B main.py ledger-initialize --db "PATH-TO-NEW-FIXTURE.db"

# Explicit V2 -> V3 migration of an existing database:
python -B main.py ledger-migrate --db "PATH-TO-V2-FIXTURE.db"
```

Send/status also accept a global `--db` before the subcommand. CLI/TUI defaults still
refer to the project's existing state path; they do not create it when missing.
Preflight is a read-only SQLite snapshot that reports schema version, request count,
transmitted count and per-record error reasons. No DDL/DML, journal-mode change,
backup or history repair is performed. SQLite may manage normal WAL reader shared-memory
coordination; read-only here means no database/schema/history changes, not filesystem
forensics immutability.

The 3A administration migration supports V2 -> V3 only. Versions 0/1 or unknown schema
versions are rejected instead of silently chaining a legacy conversion. The old V2
conversion is retained privately for fresh initialization and regression fixtures.
Any real legacy conversion requires a separately reviewed procedure and Owner permission.

Apply repeats preflight under the run lock and `BEGIN IMMEDIATE`. Before DDL it writes
a uniquely named `*.pre-v3-<uuid>.db` with SQLite Backup API using a separate committed
reader, including WAL contents. No raw database/WAL copy or forced checkpoint is used.
Backup integrity is checked. Backfill, indexes, triggers, migration record and schema
version commit together after integrity/foreign-key validation. Failed backups stop
before DDL. Failed migration retains its backup and rolls back. An already-V3 apply
does not repeat migration or create another backup.

Fresh initialization refuses overwrite using exclusive file creation. If initialization
fails after creating the empty V2 base, it reports failure and leaves that non-sendable
file for inspection; it does not silently retry, erase it or treat it as V3.

## Rollback and operational boundaries

Stop all senders before any migration/restore. A schema preflight does not authorize
runtime migration or Debian deployment. No runtime operation is approved by this document.

Restore testing uses Backup API into a different fixture and verifies original V2
rows. Never overwrite an open database or combine restored data with unrelated WAL/SHM.
Before an actual rollback, preserve a Backup API snapshot of current V3. If requests
started transmission after the pre-V3 snapshot, restoring it loses their dedupe/quota
knowledge. Do not resume sending until those records are reconciled. No destructive
automatic downgrade command is provided.

The following remain unresolved/outside 3A:

- Direct legacy send APIs in `client.py` can bypass this Safety Layer. The entire Core
  is **not** protected by these limits.
- No UNKNOWN resolution CLI, message rotation, circuit breaker, new queue worker,
  FastAPI/n8n/Web UI/systemd, or scheduler development.
- The complete interactive/noninteractive confirmation policy is still a separate phase.
- No Debian/Termux device verification or live TikTok verification.
- Correlated server echo indicates server acceptance, not recipient delivery/read
  or TikTok streak credit. TikTok endpoints are unofficial internal APIs.

## Files and verification

Production changes: `ledger.py`, `ledger_migrations.py`, `ledger_requests.py`,
`oneshot.py`, `main.py`, `services.py`, `cli.py`. Existing `tiktok_tui.py` needs no new
change in 3A: it already displays the service's shared `detail` message.

New tests: `tests/test_rate_limits.py`, `tests/ledger_fixtures.py`.
Updated regressions: `tests/test_streak.py`, `tests/test_ledger_v2.py`,
`tests/test_independent_review.py`, `tests/test_safety_fixes.py`, `tests/run_offline.py`.
Fixture setup is now explicit; old time-independent tests use simulated clocks rather
than weakening production cooldown. The offline runner also guards SQLite URI opens
against known runtime paths, including spawned multiprocessing test children.

```powershell
.\work\verify-venv\Scripts\python.exe -B tests\run_offline.py
```

Validation covers daily/account/conversation boundaries; cooldown/window before, at
and after expiry; tied timestamps; reduced Settings; UTC+7 midnight; UNKNOWN/replay/
repeat; actual process exits and restart; atomic event rollback; three separate
two-process races (daily/window/interval); read-only commands; schema gating before
credentials/network; V2->V3 WAL backup/restore; legacy preservation; invalid timestamps;
backup/migration failures; and structured CLI block output without WebSocket sends.

Latest verified result: **131 tests passed**, **40 Python files passed AST syntax
validation**, and CLI `--help` rendered successfully. Static inspection found zero
ledger/request deletion branches in the seven application/send modules checked.
Review tightened schema validation to require history triggers, the idempotency
constraint and V3 migration record, and added a regression ensuring the single-
transmitter test exercises the unique index rather than only timestamp validation.
No dependency was added. This completes 3A; implementation stops for Owner review.
