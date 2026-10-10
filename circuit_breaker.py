"""Durable manual circuit breaker. No network, retries or quota counters."""
import json
import os
import ssl
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError

import ledger


def now_ms():
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class Failure:
    category: str
    stage: str
    source: str
    code: int | None = None
    retry_not_before_ms: int | None = None

    def evidence(self):
        return dict(category=self.category, stage=self.stage, source=self.source,
                    code=self.code, retry_not_before_ms=self.retry_not_before_ms)


class ProtocolFailure(Exception):
    """Missing or malformed expected response; never carries server text."""


class CircuitOpen(ledger.LedgerError):
    def __init__(self, blocks):
        self.blocks = blocks
        super().__init__('CIRCUIT_OPEN: ' + ', '.join(
            f"{b['scope_type']} version={b['version']} {b['category']}" for b in blocks))

    def result(self):
        return {'reason': 'CIRCUIT_OPEN', 'blocks': self.blocks}


def classify(exc, stage):
    """Only typed transport errors from mandatory calls; no exception text scans."""
    from websockets.exceptions import InvalidStatus, ConnectionClosed, WebSocketException
    code, headers = None, None
    if isinstance(exc, HTTPError):
        code, headers = exc.code, exc.headers
    elif isinstance(exc, InvalidStatus):
        code, headers = exc.response.status_code, exc.response.headers
    if type(code) is int:
        category = ('AUTH' if code == 401 else 'ACCESS_DENIED_UNCLASSIFIED' if code == 403
                    else 'SERVER_RATE_LIMIT' if code == 429 else 'SERVER_UNAVAILABLE'
                    if 500 <= code <= 599 else 'SERVER_ERROR_UNCLASSIFIED')
        retry = None
        if code == 429 and headers:
            try:
                value = headers.get('Retry-After')
                retry = now_ms() + int(value) * 1000 if value.isdecimal() else int(parsedate_to_datetime(value).timestamp() * 1000)
                if not now_ms() <= retry <= 253402300799999:
                    retry = None
            except (ValueError, TypeError, AttributeError, OverflowError):
                pass
        return Failure(category, stage, 'http', code, retry)
    if isinstance(exc, URLError):
        if isinstance(exc.reason, ssl.SSLError):
            return Failure("TLS", stage, "transport")
        return Failure("NETWORK", stage, "transport")
    if isinstance(exc, ssl.SSLError):
        return Failure('TLS', stage, 'transport')
    if isinstance(exc, ConnectionClosed):
        code = exc.rcvd.code if exc.rcvd is not None else None
        return Failure('NETWORK', stage, 'websocket', code)
    if isinstance(exc, (OSError, TimeoutError, WebSocketException)):
        return Failure('NETWORK', stage, 'transport')
    if isinstance(exc, ProtocolFailure):
        return Failure('PROTOCOL', stage, 'decoder')
    return None


def scopes(uid=None, session=None):
    return ([('account', str(uid))] if uid is not None else []) + ([('session', session)] if session else [])


def check(conn, uid=None, session=None):
    blocks = []
    if session:
        binding = conn.execute("SELECT canonical_uid FROM breaker_state WHERE scope_type='session' AND scope_key=?", (session,)).fetchone()
        if binding and binding[0] and uid is None:
            uid = binding[0]
    for kind, key in scopes(uid, session):
        row = conn.execute('SELECT * FROM breaker_state WHERE scope_type=? AND scope_key=?', (kind, key)).fetchone()
        if row and (row['state'] == 'OPEN' or now_ms() < row['updated_at_ms']):
            block = dict(row)
            if now_ms() < row['updated_at_ms']:
                block['category'] = 'CLOCK_SKEW'
            blocks.append(block)
    if blocks:
        raise CircuitOpen(blocks)


def trip(conn, kind, key, failure, request_id=None, *, event_type='TRIPPED', at_ms=None):
    """Caller owns transaction; UNKNOWN and OPEN must commit together."""
    instant = now_ms() if at_ms is None else at_ms
    old = conn.execute('SELECT version,updated_at_ms FROM breaker_state WHERE scope_type=? AND scope_key=?', (kind, str(key))).fetchone()
    version = old[0] + 1 if old else 1
    instant = max(instant, old[1] if old else 0)
    conn.execute('''INSERT INTO breaker_state(scope_type,scope_key,state,version,category,opened_at_ms,updated_at_ms,retry_not_before_ms,source_request_id) VALUES(?,?,'OPEN',?,?,?,?,?,?)
        ON CONFLICT(scope_type,scope_key) DO UPDATE SET state='OPEN',version=excluded.version,
        category=excluded.category,updated_at_ms=excluded.updated_at_ms,
        retry_not_before_ms=excluded.retry_not_before_ms,source_request_id=excluded.source_request_id''',
        (kind, str(key), version, failure.category, instant, instant, failure.retry_not_before_ms, request_id))
    conn.execute('''INSERT INTO breaker_events(scope_type,scope_key,version,event_type,at_ms,evidence_json,reason,source_request_id)
        VALUES(?,?,?,?,?,?,?,?)''', (kind, str(key), version, event_type, instant,
        json.dumps(failure.evidence(), sort_keys=True), failure.category, request_id))


def record_failure(failure, *, db_path, uid=None, session=None):
    from ledger_requests import transaction
    with transaction(db_path) as conn:
        trip(conn, 'account' if uid is not None else 'session', str(uid) if uid is not None else session, failure)


def inspect(*, db_path, uid=None, session=None, history=False):
    ledger.check_ready(db_path)
    conn = ledger._get_readonly_connection(db_path)
    import sqlite3
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        result = []
        for kind, key in scopes(uid, session):
            table = 'breaker_events' if history else 'breaker_state'
            result.extend(dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE scope_type=? AND scope_key=?', (kind, key)))
        return result
    finally:
        conn.close()


def gate(*, db_path, uid=None, session=None):
    ledger.check_ready(db_path)
    import sqlite3
    conn = ledger._get_readonly_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        check(conn, uid, session)
    finally:
        conn.close()


def reset(kind, key, preview, reason, *, db_path):
    """Only the explicitly selected scope is reset. Other scopes remain barriers."""
    from ledger_requests import transaction, require_reason
    require_reason(reason)
    if kind not in ('account', 'session') or not key:
        raise ValueError('Invalid circuit scope')
    with ledger.RunLock(os.path.join(os.path.dirname(os.path.abspath(db_path)), 'run.lock')):
        with transaction(db_path) as conn:
            row = conn.execute('SELECT * FROM breaker_state WHERE scope_type=? AND scope_key=?', (kind, key)).fetchone()
            if not row or dict(row) != preview:
                raise ledger.LedgerError('Stale circuit preview; review again')
            # Session->UID identity is not guessed. Conservatively block session reset
            # for any unresolved request; account reset uses the exact UID.
            sql = "SELECT 1 FROM send_requests WHERE status IN ('pending','failed_unknown')"
            params = ()
            if kind == 'account':
                sql += ' AND canonical_uid=?'
                params = (key,)
            if conn.execute(sql, params).fetchone():
                raise ledger.LedgerError('Pending/UNKNOWN blocks reset; request history is unchanged')
            instant = now_ms()
            if instant < row['updated_at_ms']:
                raise ledger.LedgerError('CLOCK_SKEW: reset denied')
            if row['state'] != 'OPEN':
                raise ledger.LedgerError('Circuit is not OPEN')
            version = row['version'] + 1
            conn.execute("UPDATE breaker_state SET state='CLOSED',version=?,updated_at_ms=? WHERE scope_type=? AND scope_key=?", (version, instant, kind, key))
            conn.execute('''INSERT INTO breaker_events(scope_type,scope_key,version,event_type,at_ms,evidence_json,reason)
                VALUES(?,?,?,'RESET',?,'{}',?)''', (kind, key, version, instant, reason))


def bind_session(uid, session, *, db_path):
    """Remember only a server-verified identity, never credentials or config UID."""
    from ledger_requests import transaction
    with transaction(db_path) as conn:
        check(conn, uid, session)
        old = conn.execute("SELECT canonical_uid FROM breaker_state WHERE scope_type='session' AND scope_key=?", (session,)).fetchone()
        if old and old[0] and old[0] != str(uid):
            raise ledger.LedgerError('Session identity changed; use a distinct session name')
        if old and old[0] == str(uid):
            return
        instant = now_ms()
        conn.execute("""INSERT INTO breaker_state(scope_type,scope_key,state,version,category,opened_at_ms,updated_at_ms,canonical_uid)
            VALUES('session',?,'CLOSED',1,'IDENTITY_BOUND',?,?,?)
            ON CONFLICT(scope_type,scope_key) DO UPDATE SET canonical_uid=excluded.canonical_uid,
            version=version+1,updated_at_ms=excluded.updated_at_ms""", (session, instant, instant, str(uid)))
        version = conn.execute("SELECT version FROM breaker_state WHERE scope_type='session' AND scope_key=?", (session,)).fetchone()[0]
        conn.execute("""INSERT INTO breaker_events(scope_type,scope_key,version,event_type,at_ms,evidence_json,reason)
            VALUES('session',?,?,'IDENTITY_BOUND',?,'{}','SERVER_VERIFIED')""", (session, version, instant))
