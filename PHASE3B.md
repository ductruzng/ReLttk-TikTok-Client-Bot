# Phase 3B — Message Template Rotation

Owner-approved scope: message rotation, cancellation of untransmitted queued
requests, immutable snapshot review, and explicit selection skip. Implementation
and tests use fixtures only. No runtime migration, session access, TikTok send,
Git operation or Debian deployment was performed.

## Configuration and selection

Existing `message` configurations remain supported. To use rotation, omit
`message` and configure one campaign per plan:

```json
{
  "session": "example_account",
  "campaign_id": "daily-streak",
  "rotation": {
    "mode": "round_robin",
    "templates": [
      {"id": "morning", "text": "Chào buổi sáng 🌞"},
      {"id": "hello", "text": "Chúc bạn một ngày vui!"}
    ]
  },
  "targets": [{"conv_id": "EXAMPLE", "conv_short_id": 1, "conv_type": 1}]
}
```

`round_robin` is the default; `random_no_immediate_repeat` is also accepted.
Template IDs must be unique, and duplicate exact content is rejected. Blank
text, NUL and invalid UTF-8 text are rejected. Valid whitespace, Unicode and
emoji are preserved: no trimming or generated variations. Revision is SHA-256
of the exact UTF-8 text. The catalog fingerprint covers campaign, mode and
ordered templates, including revisions and text.

Selection occurs in the enqueue transaction. One queued/pending rotation request
is allowed per `(account, conversation, campaign)`. Replaying the same key and
canonical intent returns the existing request before scope admission or RNG;
different intent conflicts. Catalog changes count as changed intent. Resume an
existing request by its ID rather than recreating it with a changed catalog.

Round-robin selects the successor of the last successful template ID in the
current order; when that ID is absent, it starts at the first template. Random
excludes the last successful ID and exact text. A single-template catalog repeats
its sole template; an otherwise empty candidate set fails closed.

FAILED before transmission and CANCELLED retain the exact choice across new
keys and restarts. The choice is read from immutable requests for the current
`(success_seq, selection_generation)`. Only committed SUCCESS increments
`success_seq`. Duplicate confirmation with the same server ID does not increment
it again. SUCCESS means correlated server echo, not proof of recipient delivery,
reading, or TikTok streak credit.

## SQLite V4

One database remains in use. `send_requests` remains the only quota source.

Added request columns:

| Column | Purpose |
| --- | --- |
| `campaign_id` | Rotation scope; NULL for legacy/fixed-message requests |
| `rotation_seq` | SUCCESS sequence at enqueue |
| `selection_generation` | Administrative selection version at enqueue |
| `snapshot_json` | Selected template ID, exact text, revision, original catalog/fingerprint |
| `intent_json` | Canonical caller payload and catalog for idempotency comparison |

`payload_json.message` holds the actual selected text and is used by both packet
construction and echo correlation. Snapshot and identity columns are immutable.
`cancelled` is a terminal status. A partial unique index prevents two active
rotation requests in the same scope. Additional triggers reject incomplete
rotation metadata and cancellation of a transmitted/nonqueued request.

`rotation_state` has the scope primary key, `success_seq`,
`selection_generation`, and `last_success_request_id` referencing the request.
Initial state can be created at enqueue with sequence zero; this is not SUCCESS.
Only the SUCCESS transaction advances progress. Skip only changes generation.

`rotation_events` is append-only and records SUCCESS and SELECTION_SKIPPED with
scope, timestamp and details. Existing `request_events` stores creation,
cancellation and snapshot approval, including mandatory reasons. Old requests,
UNKNOWN markers, legacy ledger and audit history are never deleted to repeat or
skip a message.

Enqueue, cancellation, snapshot approval, claim, SUCCESS and skip use SQLite
`BEGIN IMMEDIATE`. Claim checks the current rotation version and snapshot before
the unchanged Phase 3A quota checks and transmission marker. SUCCESS updates the
request, cursor and audit atomically with an expected-version predicate.

## Explicit migration and rollback

The send path requires an existing valid V4 database and does not initialize or
migrate. Dry-run remains configuration-only and does not choose a template or
write SQLite. Read-only preflight reports version, record counts and errors.

Administration commands (examples only, not executed on runtime data):

```powershell
python -B main.py ledger-preflight --db "D:\fixtures\ledger.db"
python -B main.py ledger-initialize --db "D:\fixtures\new-ledger.db"
python -B main.py ledger-migrate --db "D:\fixtures\ledger.db"
```

V3 → V4 holds the run lock and SQLite writer transaction, repeats preflight, and
creates an exclusive backup file using SQLite Backup API with a separate reader.
This includes committed WAL pages. It rebuilds `send_requests` to extend the
status CHECK, copies all old columns, restores its indexes/triggers, adds V4
structures and validates integrity and foreign keys before commit. FK enforcement
is disabled only on the dedicated migration connection before the transaction;
foreign-key checks are explicit before commit. No writable_schema manipulation.

Legacy V3 requests get NULL rotation metadata, not invented template history.
The explicit V2 administration route still performs V2 → V3 → V4 in the same
apply transaction, with a backup of the original V2 database. Unsupported old
versions and missing/invalid historical timestamps fail preflight without repair.
Repeated migration on V4 validates and returns unchanged.

Failure before commit rolls back schema, triggers, data and version; the backup
remains available. Backup API restoration to a separate fixture file is tested.
There is no automatic down-migration. After V4 has accepted new writes, restoring
a pre-migration backup would omit those writes: preserve the complete V4 database
and reconcile explicitly before any runtime rollback. An old binary must not be
pointed at V4 as a downgrade strategy.

## Cancellation, stale snapshots and skip

Cancel requires queued status, transmitted=0 and a NULL transmission timestamp.
It records a reason and frees the active scope without dropping the sticky choice.
Pending, UNKNOWN and SUCCESS cannot be cancelled. A replay of a cancelled request
does not execute it.

Any catalog edit, removal, addition, reorder or mode change holds the old queued
snapshot. It is never silently rewritten. Approval binds request ID, snapshot
fingerprint and the currently reviewed catalog fingerprint. CLI shows the old
snapshot and current catalog. A differing fingerprint at approval or claim rejects
the operation. `--yes` does not approve old content. Config files are not versioned
by the database: changing a file away and back to identical contents yields the
same fingerprint. File edits after the transmission marker commits cannot revoke
that transmission.

`rotation-skip-selection` requires an interactive terminal, preview, a nonblank
reason and typing the command name. Under the writer transaction it rechecks the
preview, requires no active queued/pending request in the scope and no unresolved
UNKNOWN in the account/conversation. It increments generation and appends an audit
event atomically. No SUCCESS/quota is changed and no send is triggered. With no
uncompleted choice, repeated skip is rejected. A new request then selects from the
current catalog using the last SUCCESS; this can legitimately choose the same
template again. No automatic re-roll path exists.

## CLI examples

These commands illustrate administration; they were not run on real data:

```powershell
python -B main.py request-show --db "D:\fixtures\ledger.db" --request-id REQUEST_ID
python -B main.py request-cancel --db "D:\fixtures\ledger.db" --request-id REQUEST_ID
python -B main.py request-approve-snapshot --db "D:\fixtures\ledger.db" --request-id REQUEST_ID --config plan.json
python -B main.py rotation-skip-selection --db "D:\fixtures\ledger.db" --account ACCOUNT_UID --conversation CONV_ID --campaign daily-streak
```

Cancel/approval/skip prompt for a reason and confirmation. Approval does not send.
There is no noninteractive skip or snapshot-approval command in this phase.
Request inspection is read-only and does not require credentials.

For a future Owner-authorized live run, `--send` supports `--request-id` to resume
the exact queued snapshot. Interactive CLI previews the actual request and asks
for `SEND`. Noninteractive sends require `--yes --idempotency-key`; when resuming,
use the exact stored request key from `request-show`, including any JSON encoding.
New batch sends derive each target's key from `[batch_key, conversation_id]`.
Repeat requires a new key and explicit Settings allowing it. None of these options
bypass UNKNOWN, daily quota, interval or sliding-window limits.

CLI and TUI use `services.send_plan`. The existing CLI menu now also previews the
actual request. Manual TUI sends preview and leave the request queued for CLI
confirmation, avoiding a new confirmation interface. Its plan editor refuses to
overwrite a rotation configuration with a fixed message. The pre-existing
scheduler was not developed or activated; it remains outside this phase's review.

## Offline verification

Command: `.\work\verify-venv\Scripts\python.exe -B tests\run_offline.py`.
The runner blocks external networking and runtime session/config/database paths.
Tests use temporary fixtures and mocked WebSockets only.

Coverage includes round-robin/random/single-template, independent scopes,
idempotency and intentional repeat, sticky FAILED/CANCELLED, UNKNOWN blocking,
catalog edits and stale approvals, exact Unicode packet/echo content, cancel and
skip audit rollback, crash recovery after uncommitted SUCCESS, multiprocessing
enqueue/cancel/claim/skip/SUCCESS races, V3 WAL backup and restore, migration
rollback and repeat application, plus the Phase 2/3A regression suite.

Final result: **160 PASS, 0 FAIL** (131 existing regressions and 29 added Phase 3B
tests). AST parsing: **42 Python files PASS**. `main.py --help` exits successfully.
Textual emitted slow-task diagnostics during its existing UI tests; no test failed.

## Changed files

- New: `rotation.py`, `tests/test_rotation.py`, `PHASE3B.md`.
- Ledger/schema: `ledger.py`, `ledger_requests.py`, `ledger_migrations.py`.
- Shared send/CLI/TUI: `oneshot.py`, `services.py`, `main.py`, `cli.py`, `tiktok_tui.py`.
- Existing regression updates: `tests/test_rate_limits.py` compares the preserved
  old columns explicitly now that V4 appends metadata; `tests/test_ledger_v2.py`
  supplies required noninteractive `--yes` consent in its CLI forwarding test.

## Remaining limits

- No real TikTok validation or Termux/Android device execution was performed.
- TikTok uses an unofficial internal API. Bot messages counting toward streaks
  remain unverified. Rate-limit settings are internal policy, not guaranteed bot
  detection avoidance thresholds.
- A crash after receiving echo but before committing SUCCESS becomes UNKNOWN
  on recovery, with unchanged rotation and retained quota. No automatic retry.
- UNKNOWN resolution CLI, Circuit Breaker, queue worker, scheduler development,
  FastAPI, n8n, Web UI and deployment are outside scope.
- Legacy send APIs outside this shared service remain an unresolved bypass risk;
  this phase does not claim the whole Core is protected.
- Skip reasons/history provide accountability, not an authorization boundary
  against someone who can directly modify the SQLite file or call Python APIs.
- Holding an interactive confirmation while connected can let the remote socket
  expire; subsequent uncertainty is handled conservatively by the existing send
  boundary and UNKNOWN policy.

Stop after Phase 3B for Owner review. No Phase 3C implementation is authorized.

## Independent review follow-up: phase3b-hardening

Completed the approved CLI fixes without changing V4 schema:

- Root cause: the legacy menu wrote `message` directly without checking rotation.
  `cli.menu_config_message` now refuses rotation plans before prompting for a new
  message. `services.save_fixed_message` rereads and validates message/rotation and
  rate settings before the atomic write. Invalid input leaves the file untouched;
  fixed-message plans remain editable, including partially configured plans.
- Root cause: `cmd_send` treated every skipped operation as successful and labelled
  all other failures UNKNOWN. It now returns 0 only for a nonempty batch whose
  every result is confirmed or a skipped replay with `existing_status=confirmed`.
  Quota, cooldown, held snapshot, declined confirmation, queued/pending,
  cancelled, pretransmit failure, UNKNOWN and unclassified skips return 1.
- Existing machine-readable result dictionaries remain compatible. Skipped paths
  now include `existing_status`; confirmed replay also includes `server_msg_id`.
  Human output distinguishes the skipped operation from its durable request state
  and prints each failure's actual status. No retry or extra transmission added.

Changed: `cli.py`, `services.py`, `main.py`, `oneshot.py`, this document.
Added: `tests/test_phase3b_hardening.py` (8 tests, with subcases).
Full offline regression result: **168 PASS, 0 FAIL**, including all 160 prior tests.
Output captured in `work/phase3b-hardening-tests.txt` for C2C review.

Remaining limitations above are unchanged. No runtime/session access, Git,
migration, live TikTok, deployment or Phase 3C work was performed in this follow-up.
The subprocess status is completion/noncompletion (0/1), not a granular error-code
taxonomy; inspect structured per-target fields for the specific reason.
