# Phase 3C — manual circuit breaker

Implementation submitted for independent review; not production acceptance.

## Behavior

`CLOSED` permits the existing safety checks. The first qualifying mandatory
transport failure sets `OPEN`; it stays OPEN across restart and time boundaries.
There is no automatic retry, probe, HALF_OPEN or automatic reset.

The canonical account UID is verified from the server on the send path (the old
UID cache is not used as identity evidence). Before identity is available, a
session-name circuit blocks pre-auth failures. A session-to-UID binding is stored
after verification, allowing subsequent runs to check that account before network
activity. Changing the identity behind an already bound session name is rejected;
use a distinct session name. This stores no session credentials.

The existing RunLock now encloses recovery, authentication, inbox validation and
transport. Applicable account/session barriers are rechecked inside the SQLite
claim transaction, before the transmission marker. Quota errors retain their
existing detailed diagnostics; either a quota rejection or a circuit rejection
leaves the request queued and untransmitted.

UNKNOWN, OPEN and a sanitized audit event commit in one transaction. Recovery of
pending requests does the same under RunLock. A failure of that transaction leaves
pending intact and aborts the operation; the next run must recover before sending.
Cancellation during send/echo also records UNKNOWN before propagating cancellation.
Confirmed in-flight requests never close a separately opened circuit. UNKNOWN
preserves quota and rotation; a trip stops the remaining batch without enqueueing
new rotation selections. Incomplete batches retain a nonzero CLI exit status.

## Error policy and evidence limitations

| Evidence from mandatory operation | Classification |
| --- | --- |
| HTTP 401 | AUTH |
| HTTP 403 | ACCESS_DENIED_UNCLASSIFIED (not a ban assertion) |
| HTTP 429 | SERVER_RATE_LIMIT |
| HTTP 5xx | SERVER_UNAVAILABLE |
| Other HTTP rejection | SERVER_ERROR_UNCLASSIFIED |
| Typed TLS failure | TLS; verification stays enabled |
| Typed timeout, DNS/connect/reset, WebSocket exception | NETWORK |
| Missing expected identity evidence | PROTOCOL |
| No correlated echo after transmission | UNKNOWN |

Before the marker, these failures open a known account or pre-auth session scope
without consuming transmission quota. After the marker they also leave the request
UNKNOWN. Valid Retry-After is only a reevaluation hint, not a scheduling instruction.
Local validation, missing credentials, quota and snapshot holds are not server
restriction evidence. Clock rollback behind a circuit's latest state timestamp
blocks claims/reset; clock jumps never automatically release an OPEN circuit.

Raw keyword detection of `ban`, `error`, `restricted`, etc. was removed. Ordinary
message text containing those words can now correlate successfully. Unsupported,
unrelated or malformed binary frames do not confirm a send; absent a matching echo,
the request times out to UNKNOWN. Existing protobuf correlation still requires
UID, conversation, client ID, positive server message ID and exact snapshot text.

**Unresolved protocol evidence:** the repository has no independently verified
TikTok WebSocket restriction/challenge/error-envelope schema or numeric mappings.
No mapping has been invented. Thus protocol-level restriction classification and
the proposed contradictory verified-error-plus-echo check are not implemented.
The synthetic JSON error fixture is not treated as proof of such a schema.
This gap needs Technical Owner review and a verified offline fixture/spec before
those error-specific rules can be added safely. HTTP classification is implemented.

Breaker evidence stores category, stage, source, integer code and retry hint only;
no raw frames, exception strings, response body, headers, tokens or message text.
Owner-entered reset reasons are intentionally retained; do not put credentials in
them. Existing legacy logging outside this path has not been comprehensively audited.

## SQLite V5

`send_requests` remains the only transmission/quota source. V4 request, rotation
and audit rows are preserved without modifications.

- `breaker_state`: primary key `(scope_type, scope_key)`; state, monotonic version,
  category, opened/updated UTC epoch milliseconds, optional retry hint/request FK,
  optional verified canonical UID for session binding.
- `breaker_events`: append-only event ID, scope/version/type/time, sanitized JSON
  evidence, reason and optional request FK. UPDATE/DELETE are rejected by triggers.
- Index `breaker_scope_history` supports scope history lookup.

Explicit readonly preflight reports UNKNOWN and pending counts. Apply holds run
and SQLite writer locks, uses the SQLite Backup API with a separate committed WAL
reader, then adds V5 structures and imports each historical UNKNOWN as an OPEN
account event linked to its original request. Event timestamps describe migration
time, not invented historical failure times. Pending remains pending until locked
recovery. Invalid preflight aborts. Validation occurs before commit; failed apply
rolls back all DDL/data changes. Already-valid V5 migration is a no-op.

There is no auto-init/migrate in send, status or dry-run. Sending requires V5.
Initialization/migration commands remain explicit administration. No real database
was opened or migrated during this implementation.

Rollback: stop all users of the database and restore the Backup API snapshot to a
separate verified path. The backup is V4 when upgrading V4. Do not overwrite an
active DB or discard V5 writes: preserve them and reconcile first. No automatic
downgrade or destructive restore command was added.

## CLI (offline administration)

Examples below use an explicitly selected disposable fixture database:

```powershell
python -B main.py ledger-preflight --db work/fixture.db
python -B main.py ledger-migrate --db work/fixture.db
python -B main.py breaker-status --db work/fixture.db --account 111
python -B main.py breaker-history --db work/fixture.db --session fixture
python -B main.py breaker-reset --db work/fixture.db --account 111
```

Status/history return JSON. Reset requires a TTY, preview, nonblank reason and
typing `RESET`. It compares the whole preview/version under transaction and holds
RunLock. Stale preview, clock rollback or pending/UNKNOWN denies reset. No `--yes`
override exists. Only the selected scope resets; the other scope remains a gate.
Session reset conservatively checks **all** unresolved pending/UNKNOWN because
pre-auth sessions may have no verified UID binding; this can block unrelated
session resets and is an explicit availability limitation. There is no UNKNOWN
resolution command in Phase 3C.

## Changes and checks

New: `circuit_breaker.py`, `tests/test_circuit_breaker.py`, `PHASE3C.md`.

Updated: `ledger.py`, `ledger_requests.py`, `ledger_migrations.py`, `oneshot.py`,
`client.py`, `core/api.py`, `main.py`, `services.py`, and regression expectations
in `tests/test_rotation.py`, `tests/test_streak.py`, `tests/test_independent_review.py`.

- Full offline suite: **188 PASS, 0 FAIL** (168 existing + 20 circuit tests),
  11.600 seconds. Evidence: `work/phase3c-tests.txt`.
- Syntax: 13 changed Python files parsed successfully with `ast.parse`.
- CLI `--help`: PASS.
- Tests include typed HTTP/TLS/timeout errors, redaction, restart, atomic rollback,
  UNKNOWN/quota retention, stale reset, pending reset denial, late SUCCESS,
  session binding/scope isolation, clock rollback, replay/pretransmit behavior,
  two-process trip/claim contention, V4 WAL backup/restore/repeat/rollback,
  keyword false positives, send cancellation, and batch stop without new selections.

## Remaining boundaries

Legacy direct send APIs outside the shared service remain a bypass risk. This is
not a claim that the entire Core is protected. Confirmed idempotency replay can
still perform authentication/handshake before returning the prior result. Session
aliases not yet bound to a UID cannot be linked before identity verification.
No Android/Termux, live TikTok or real account/session verification was performed.
This bot uses unofficial internal APIs; a correlated server echo is server
acknowledgment, not proof of recipient delivery or TikTok streak credit.

No Git operations, real-data migration, deployment, scheduler/worker, FastAPI,
n8n or Phase 3D changes were made. Stop for Technical Owner review.

Guarded rerun: `python -B tests/run_offline.py` — 188 PASS, 0 FAIL in 13.630s. External network and runtime-file guards enabled. Evidence: `work/phase3c-guarded-tests.txt`.


## Independent review follow-up

Technical Owner accepted the core conditionally. WS restriction/conflicting-echo
handling and broad session-reset blocking are explicitly **DEFERRED** technical
debt, not completed protocol features. No guessed mapping was added.

Fixed `core/api.py:get_own_user_id(use_cache=False)` to skip both cache reads and
writes. Two regression tests cover fresh identity success/missing identity with
all file opening forbidden, and preservation of default cached behavior using a
mock cache. Final guarded suite: **190 PASS, 0 FAIL in 12.264s** using
`python -B tests/run_offline.py`; external network/runtime file guards enabled.
Evidence: `work/phase3c-review-tests.txt`. Changed-file AST parsing also passed.
