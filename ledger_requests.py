"""Transactional request lifecycle; all writes share the existing ledger database."""
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import ledger
import rotation
import circuit_breaker as breaker


class IdempotencyConflict(ledger.LedgerError):
    pass


DEFAULT_SETTINGS = {
    'allow_repeat_same_day': False,
    'max_sends_per_conversation_per_day': 1,
    'max_sends_per_account_per_day': 20,
    'min_send_interval_seconds': 60,
    'max_sends_per_window': 5,
    'window_seconds': 300,
}


def validate_settings(allow_repeat_same_day=False, max_sends_per_conversation_per_day=1, *,
                      max_sends_per_account_per_day=20, min_send_interval_seconds=60,
                      max_sends_per_window=5, window_seconds=300):
    if type(allow_repeat_same_day) is not bool:
        raise ValueError('allow_repeat_same_day must be a boolean')
    settings = dict(allow_repeat_same_day=allow_repeat_same_day,
                    max_sends_per_conversation_per_day=max_sends_per_conversation_per_day,
                    max_sends_per_account_per_day=max_sends_per_account_per_day,
                    min_send_interval_seconds=min_send_interval_seconds,
                    max_sends_per_window=max_sends_per_window, window_seconds=window_seconds)
    for name, value in settings.items():
        if name == 'allow_repeat_same_day':
            continue
        minimum = 0 if name == 'min_send_interval_seconds' else 1
        if type(value) is not int or not minimum <= value <= 2147483647:
            raise ValueError(f'{name} must be an integer in [{minimum}, 2147483647]')
    return settings


def settings_from_config(cfg):
    return validate_settings(**{key: cfg.get(key, default) for key, default in DEFAULT_SETTINGS.items()})


def utc_now_ms():
    return time.time_ns() // 1000000


def local_time(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ledger.TZ_HO_CHI_MINH)


class RateLimitError(ledger.QuotaExceededError):
    def __init__(self, request_id, reasons):
        self.request_id = request_id
        self.reasons = reasons
        times = [r['retry_at_ms'] for r in reasons]
        self.retry_at = local_time(max(times)).isoformat() if all(t is not None for t in times) else None
        lines = [f"{r['code']}: {r['used']}/{r['limit']} ({r['scope']}); {r['detail']}" for r in reasons]
        lines.append(f"Re-evaluate at: {self.retry_at or 'unknown; review required'}. Queued; no automatic retry.")
        super().__init__(' | '.join(lines))

    def result(self):
        return {'request_id': self.request_id, 'blocks': self.reasons, 'retry_at': self.retry_at}


def now():
    return datetime.now(ledger.TZ_HO_CHI_MINH).isoformat()


@contextmanager
def transaction(db_path):
    conn = ledger._get_readwrite_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def event(conn, request_id, kind, details=None):
    conn.execute('INSERT INTO request_events(request_id,event_type,created_at,details_json) VALUES(?,?,?,?)',
                 (request_id, kind, now(), json.dumps(details or {}, sort_keys=True)))


def create_request(uid, conv_id, key, payload, *, db_path, client_msg_id=None, rotation_config=None):
    """A replay returns the same durable request before evaluating repeat policy."""
    if not all(isinstance(v, str) and v.strip() for v in (uid, conv_id, key)):
        raise ValueError('uid, conv_id and idempotency key must be nonempty strings')
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    cat = rotation.catalog(rotation_config) if rotation_config is not None else None
    intent = rotation.encode({'payload': payload, 'catalog': cat}) if cat else None
    with transaction(db_path) as conn:
        previous = conn.execute('SELECT * FROM send_requests WHERE canonical_uid=? AND idempotency_key=?', (uid, key)).fetchone()
        if previous:
            same_intent = (previous['intent_json'] == intent if cat or previous['intent_json']
                           else previous['payload_json'] == encoded)
            if previous['conv_id'] != conv_id or not same_intent:
                raise IdempotencyConflict('Idempotency key already belongs to a different payload')
            return dict(previous), False
        request_id = secrets.token_hex(16)
        snapshot, seq, generation = None, None, None
        if cat:
            scope = (uid, conv_id, cat['campaign_id'])
            if conn.execute("SELECT 1 FROM send_requests WHERE canonical_uid=? AND conv_id=? AND campaign_id=? AND status IN ('queued','pending')", scope).fetchone():
                raise ledger.LedgerError('Rotation scope has an active request; resume or cancel it')
            conn.execute('INSERT OR IGNORE INTO rotation_state(canonical_uid,conv_id,campaign_id) VALUES(?,?,?)', scope)
            state = conn.execute('SELECT * FROM rotation_state WHERE canonical_uid=? AND conv_id=? AND campaign_id=?', scope).fetchone()
            seq, generation = state['success_seq'], state['selection_generation']
            selected = conn.execute('SELECT snapshot_json FROM send_requests WHERE canonical_uid=? AND conv_id=? AND campaign_id=? AND rotation_seq=? AND selection_generation=? ORDER BY rowid LIMIT 1', (*scope, seq, generation)).fetchone()
            if selected:
                snapshot = selected[0]
            else:
                last = conn.execute('SELECT snapshot_json FROM send_requests WHERE request_id=?', (state['last_success_request_id'],)).fetchone()
                template = rotation.choose(cat, json.loads(last[0])['template'] if last else None)
                snapshot = rotation.encode({'template': template, 'catalog': cat, 'fingerprint': rotation.fingerprint(cat)})
            payload = dict(payload, message=json.loads(snapshot)['template']['text'])
            encoded = rotation.encode(payload)
        conn.execute('''INSERT INTO send_requests(request_id,canonical_uid,conv_id,idempotency_key,payload_json,
            status,created_at,client_msg_id,campaign_id,rotation_seq,selection_generation,snapshot_json,intent_json)
            VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?)''',
                     (request_id, uid, conv_id, key, encoded, now(), client_msg_id or str(uuid.uuid4()),
                      cat['campaign_id'] if cat else None, seq, generation, snapshot, intent))
        event(conn, request_id, 'CREATED')
        return dict(conn.execute('SELECT * FROM send_requests WHERE request_id=?', (request_id,)).fetchone()), True


def start_transmission(request_id, *, db_path, date=None, allow_repeat_same_day=False,
                       max_sends_per_conversation_per_day=1, max_sends_per_account_per_day=20,
                       min_send_interval_seconds=60, max_sends_per_window=5, window_seconds=300,
                       rotation_config=None, session_name=None):
    """Commit the conservative transmission boundary immediately before ws.send."""
    settings = validate_settings(allow_repeat_same_day, max_sends_per_conversation_per_day,
        max_sends_per_account_per_day=max_sends_per_account_per_day,
        min_send_interval_seconds=min_send_interval_seconds,
        max_sends_per_window=max_sends_per_window, window_seconds=window_seconds)
    with transaction(db_path) as conn:
        row = conn.execute('SELECT * FROM send_requests WHERE request_id=?', (request_id,)).fetchone()
        if not row:
            raise ledger.LedgerError('Unknown request')
        if row['status'] != 'queued':
            return False
        check_snapshot(conn, row, rotation_config)
        instant = utc_now_ms()
        local = local_time(instant)
        day = local.date().isoformat()
        if date is not None and date != day:
            raise ValueError('Explicit date must match the transmission clock in Asia/Ho_Chi_Minh')
        midnight = int((local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp() * 1000)
        args = (row['canonical_uid'], row['conv_id'])
        used = conn.execute('SELECT count(*) FROM send_requests WHERE canonical_uid=? AND conv_id=? AND date=? AND transmitted=1', (*args, day)).fetchone()[0]
        account_used = conn.execute('SELECT count(*) FROM send_requests WHERE canonical_uid=? AND date=? AND transmitted=1', (args[0], day)).fetchone()[0]
        last = conn.execute('SELECT max(transmission_started_at_ms) FROM send_requests WHERE canonical_uid=? AND transmitted=1', (args[0],)).fetchone()[0]
        window = [r[0] for r in conn.execute('SELECT transmission_started_at_ms FROM send_requests WHERE canonical_uid=? AND transmitted=1 AND transmission_started_at_ms>? ORDER BY transmission_started_at_ms DESC', (args[0], instant-window_seconds*1000))]
        reasons = []

        def block(code, scope, usage, limit, retry_at_ms, detail):
            reasons.append(dict(code=code, scope=scope, used=usage, limit=limit,
                                retry_at_ms=retry_at_ms,
                                retry_at=local_time(retry_at_ms).isoformat() if retry_at_ms is not None else None,
                                detail=detail))

        unknown = conn.execute("SELECT request_id FROM send_requests WHERE canonical_uid=? AND conv_id=? AND status='failed_unknown' LIMIT 1", args).fetchone()
        limit = max_sends_per_conversation_per_day
        if unknown:
            block('UNKNOWN', 'conversation', used, limit, None, f'{unknown[0]} blocks across dates; Owner review required')
        if used >= limit:
            block('CONVERSATION_DAILY_LIMIT', 'conversation', used, limit, midnight, 'max_sends_per_conversation_per_day; adjust Settings explicitly to allow more')
        if account_used >= max_sends_per_account_per_day:
            block('ACCOUNT_DAILY_LIMIT', 'account', account_used, max_sends_per_account_per_day, midnight, 'max_sends_per_account_per_day')
        if used and not allow_repeat_same_day:
            block('REPEAT_DISABLED', 'conversation', used, 1, midnight, 'allow_repeat_same_day=false; a new key does not bypass Settings')
        if last is not None and instant < last:
            block('CLOCK_SKEW', 'account', instant, last, None, 'Clock precedes the latest transmission; correct the clock before re-evaluation')
        if last is not None and instant < last + min_send_interval_seconds*1000:
            block('MIN_INTERVAL', 'account', max(0, (instant-last)/1000), min_send_interval_seconds,
                  last + min_send_interval_seconds*1000, 'elapsed seconds / required seconds')
        if len(window) >= max_sends_per_window:
            block('SLIDING_WINDOW', 'account', len(window), max_sends_per_window,
                  window[max_sends_per_window-1] + window_seconds*1000, f'last {window_seconds} seconds')
        if conn.execute('SELECT 1 FROM crossover_guards WHERE canonical_uid=? AND conv_id=? AND date=?', (*args, day)).fetchone():
            block('CROSSOVER_GUARD', 'conversation', 1, 1, midnight, 'Midnight crossover guard')
        if conn.execute("SELECT 1 FROM send_requests WHERE status='pending'").fetchone():
            block('TRANSMISSION_PENDING', 'database', 1, 1, None, 'concurrency=1; recovery requires the exclusive run lock')
        if reasons:
            raise RateLimitError(request_id, reasons)
        breaker.check(conn, row['canonical_uid'], session_name)
        conn.execute("UPDATE send_requests SET status='pending',date=?,attempt_time=?,transmitted=1,transmission_started_at_ms=? WHERE request_id=?", (day, local.isoformat(), instant, request_id))
        event(conn, request_id, 'TRANSMISSION_STARTED', settings)
        return True


def fail_pretransmit(request_id, *, db_path):
    with transaction(db_path) as conn:
        updated = conn.execute("UPDATE send_requests SET status='failed_pretransmit',reason_code='CLIENT_EXCEPTION' WHERE request_id=? AND status='queued'", (request_id,))
        if updated.rowcount:
            event(conn, request_id, 'FAILED_PRETRANSMIT')


def finish(uid, conv_id, day, client_id, *, db_path, status, reason, server_id=None, result_date=None, failure=None):
    with transaction(db_path) as conn:
        row = conn.execute('SELECT * FROM send_requests WHERE canonical_uid=? AND conv_id=? AND date=? AND client_msg_id=?', (uid, conv_id, day, client_id)).fetchone()
        if not row:
            raise ledger.LedgerError('No matching transmission reservation')
        if row['status'] == 'confirmed' and status == 'failed_unknown':
            return
        if row['status'] == 'confirmed' and status == 'confirmed' and row['server_msg_id'] == server_id:
            return
        if row['status'] != 'pending':
            raise ledger.LedgerError('Request is not pending; terminal history cannot be rewritten')
        conn.execute('UPDATE send_requests SET status=?,reason_code=?,server_msg_id=?,confirmed_time=? WHERE request_id=?',
                     (status, reason, server_id, now() if status == 'confirmed' else None, row['request_id']))
        event(conn, row['request_id'], status.upper(), {'reason': reason, 'server_msg_id': server_id})
        if status == 'failed_unknown':
            breaker.trip(conn, 'account', uid, failure or breaker.Failure('UNKNOWN', 'echo', 'ledger'), row['request_id'])
        if status == 'confirmed' and row['campaign_id'] is not None:
            updated = conn.execute('''UPDATE rotation_state SET success_seq=success_seq+1,last_success_request_id=?
                WHERE canonical_uid=? AND conv_id=? AND campaign_id=? AND success_seq=? AND selection_generation=?''',
                (row['request_id'], uid, conv_id, row['campaign_id'], row['rotation_seq'], row['selection_generation']))
            if updated.rowcount != 1:
                raise ledger.LedgerError('Stale rotation version; SUCCESS transaction rolled back')
            rotation_event(conn, (uid, conv_id, row['campaign_id']), 'SUCCESS', {'request_id': row['request_id']})
        if result_date and result_date != day:
            conn.execute('INSERT OR IGNORE INTO crossover_guards VALUES(?,?,?,?)', (uid, conv_id, result_date, row['request_id']))


def recover_pending(*, db_path, run_lock):
    """Require the database's exclusive process lock before classifying a crash."""
    expected = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'run.lock')
    if not isinstance(run_lock, ledger.RunLock) or run_lock._fd is None or os.path.abspath(run_lock.lock_path) != expected:
        raise ledger.LockError('Crash recovery requires the held database run lock')
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT request_id,canonical_uid FROM send_requests WHERE status='pending'").fetchall()
        for row in rows:
            conn.execute("UPDATE send_requests SET status='failed_unknown',reason_code='UNKNOWN' WHERE request_id=?", (row[0],))
            event(conn, row[0], 'CRASH_RECOVERED_UNKNOWN')
            breaker.trip(conn, 'account', row[1], breaker.Failure('UNKNOWN', 'recovery', 'ledger'), row[0])
        return len(rows)


def get_request(request_id, *, db_path):
    conn = ledger._get_readonly_connection(db_path)
    if conn is None:
        raise ledger.LedgerError('Request database does not exist')
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM send_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise ledger.LedgerError('Unknown request')
        return dict(row)
    finally:
        conn.close()


def rotation_event(conn, scope, kind, details):
    conn.execute('INSERT INTO rotation_events(canonical_uid,conv_id,campaign_id,event_type,created_at,details_json) VALUES(?,?,?,?,?,?)',
                 (*scope, kind, now(), rotation.encode(details)))


def require_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('An explicit reason is required')


def require_queued(row):
    if not row or row['status'] != 'queued' or row['transmitted'] or row['transmission_started_at_ms'] is not None:
        raise ledger.LedgerError('Only queued requests that never started transmission can be changed')


def cancel_request(request_id, reason, *, db_path):
    require_reason(reason)
    with transaction(db_path) as conn:
        row = conn.execute('SELECT * FROM send_requests WHERE request_id=?', (request_id,)).fetchone()
        require_queued(row)
        conn.execute("UPDATE send_requests SET status='cancelled',reason_code='OWNER_CANCELLED' WHERE request_id=?", (request_id,))
        event(conn, request_id, 'CANCELLED', {'reason': reason})


def snapshot_review(row, config):
    cat = rotation.catalog(config)
    if not row['snapshot_json'] or not cat or cat['campaign_id'] != row['campaign_id']:
        raise ledger.LedgerError('Snapshot review requires the same rotation campaign')
    return dict(request_id=row['request_id'], snapshot_fingerprint=rotation.fingerprint(json.loads(row['snapshot_json'])),
                config_fingerprint=rotation.fingerprint(cat))


def check_snapshot(conn, row, config):
    if row['campaign_id'] is None:
        return
    state = conn.execute('SELECT success_seq,selection_generation FROM rotation_state WHERE canonical_uid=? AND conv_id=? AND campaign_id=?',
                         (row['canonical_uid'], row['conv_id'], row['campaign_id'])).fetchone()
    if not state or tuple(state) != (row['rotation_seq'], row['selection_generation']):
        raise ledger.LedgerError('Stale rotation version')
    if config is None:
        raise ledger.LedgerError('Current rotation configuration is required before transmission')
    review = snapshot_review(row, config)
    if review['config_fingerprint'] == json.loads(row['snapshot_json'])['fingerprint']:
        return
    for approved in conn.execute("SELECT details_json FROM request_events WHERE request_id=? AND event_type='SNAPSHOT_APPROVED'", (row['request_id'],)):
        if json.loads(approved[0]).get('review') == review:
            return
    raise ledger.LedgerError('TEMPLATE_CONFIG_CHANGED: queued snapshot held; use request-show, request-approve-snapshot or request-cancel')


def approve_snapshot(request_id, config, expected_review, reason, *, db_path):
    require_reason(reason)
    with transaction(db_path) as conn:
        row = conn.execute('SELECT * FROM send_requests WHERE request_id=?', (request_id,)).fetchone()
        require_queued(row)
        review = snapshot_review(row, config)
        if review != expected_review:
            raise ledger.LedgerError('Stale snapshot preview; review again')
        event(conn, request_id, 'SNAPSHOT_APPROVED', {'review': review, 'reason': reason})


def _skip_preview(conn, scope):
    state = conn.execute('SELECT * FROM rotation_state WHERE canonical_uid=? AND conv_id=? AND campaign_id=?', scope).fetchone()
    if not state:
        raise ledger.LedgerError('No selection to skip')
    if conn.execute("SELECT 1 FROM send_requests WHERE canonical_uid=? AND conv_id=? AND campaign_id=? AND status IN ('queued','pending')", scope).fetchone():
        raise ledger.LedgerError('Cancel queued request first; pending cannot be skipped')
    if conn.execute("SELECT 1 FROM send_requests WHERE canonical_uid=? AND conv_id=? AND status='failed_unknown'", scope[:2]).fetchone():
        raise ledger.LedgerError('Unresolved UNKNOWN blocks selection skip')
    row = conn.execute('SELECT request_id,snapshot_json FROM send_requests WHERE canonical_uid=? AND conv_id=? AND campaign_id=? AND rotation_seq=? AND selection_generation=? ORDER BY rowid LIMIT 1',
                       (*scope, state['success_seq'], state['selection_generation'])).fetchone()
    if not row:
        raise ledger.LedgerError('No selection to skip')
    return dict(scope=list(scope), success_seq=state['success_seq'], selection_generation=state['selection_generation'],
                last_success_request_id=state['last_success_request_id'], request_id=row['request_id'], snapshot=json.loads(row['snapshot_json']))


def preview_skip(scope, *, db_path):
    ledger.check_ready(db_path)
    conn = ledger._get_readonly_connection(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        return _skip_preview(conn, scope)
    finally:
        conn.close()


def skip_selection(scope, expected_preview, reason, *, db_path):
    """Explicit administration only; never invoked by enqueue or send."""
    require_reason(reason)
    with transaction(db_path) as conn:
        actual = _skip_preview(conn, scope)
        if actual != expected_preview:
            raise ledger.LedgerError('Stale rotation preview; review again')
        conn.execute('UPDATE rotation_state SET selection_generation=selection_generation+1 WHERE canonical_uid=? AND conv_id=? AND campaign_id=?', scope)
        rotation_event(conn, scope, 'SELECTION_SKIPPED', {'preview': actual, 'reason': reason,
                                                       'new_generation': actual['selection_generation'] + 1})
