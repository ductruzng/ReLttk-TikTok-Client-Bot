"""Explicit versioned migrations preserving history, including the V4 table rebuild."""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

VERSION = 5


def _migrate_v2(conn, db_path):
    """Hold the writer lock while backing up a separate committed WAL snapshot."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
            conn.commit()
            return
        if version not in (0, 1):
            raise ValueError("Unsupported ledger schema version")
        legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_ledger'").fetchone()
        backup_path = None
        if legacy:
            backup_path = str(Path(db_path).absolute()) + f".pre-v2-{uuid.uuid4().hex}.db"
            fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            # Do not back up through conn: it owns a write transaction. A separate
            # reader includes committed WAL pages without checkpointing/copying files.
            source = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True)
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Ledger backup integrity check failed")
            finally:
                destination.close()
                source.close()
        else:
            conn.execute("""CREATE TABLE daily_ledger (
                canonical_uid TEXT NOT NULL, conv_id TEXT NOT NULL, date TEXT NOT NULL,
                status TEXT NOT NULL, attempt_time TEXT NOT NULL, client_msg_id TEXT NOT NULL,
                server_msg_id TEXT, confirmed_time TEXT, reason_code TEXT,
                PRIMARY KEY(canonical_uid,conv_id,date))""")

        statements = [
            """CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, backup_path TEXT)""",
            """CREATE TABLE send_requests(
                request_id TEXT PRIMARY KEY, canonical_uid TEXT NOT NULL, conv_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','pending','confirmed','failed_unknown','failed_pretransmit')),
                created_at TEXT NOT NULL, date TEXT, attempt_time TEXT, client_msg_id TEXT NOT NULL,
                server_msg_id TEXT, confirmed_time TEXT, reason_code TEXT,
                transmitted INTEGER NOT NULL DEFAULT 0 CHECK(transmitted IN (0,1)),
                legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1)),
                UNIQUE(canonical_uid, idempotency_key))""",
            "CREATE INDEX request_quota ON send_requests(canonical_uid,conv_id,date,transmitted)",
            "CREATE UNIQUE INDEX request_client_identity ON send_requests(canonical_uid,client_msg_id) WHERE legacy=0",
            "CREATE UNIQUE INDEX single_transmitter ON send_requests((1)) WHERE status='pending'",
            """CREATE TABLE request_events(
                event_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL REFERENCES send_requests(request_id),
                event_type TEXT NOT NULL, created_at TEXT NOT NULL, details_json TEXT NOT NULL)""",
            """CREATE TABLE crossover_guards(canonical_uid TEXT NOT NULL, conv_id TEXT NOT NULL,
                date TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES send_requests(request_id),
                PRIMARY KEY(canonical_uid,conv_id,date))""",
        ]
        for statement in statements:
            conn.execute(statement)
        now = datetime.now(timezone.utc).isoformat()
        for row in conn.execute("SELECT * FROM daily_ledger").fetchall():
            uid, cid, day, status, attempted, client_id, server_id, confirmed, reason = row
            # Legacy never stored content or a reliable transmission boundary.
            # Preserve every source field in daily_ledger and the import event.
            request_id = "legacy:" + uuid.uuid4().hex
            mapped = 'confirmed' if status == 'confirmed' else 'failed_unknown'
            conn.execute("""INSERT INTO send_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (request_id, uid, cid, request_id, '{}', mapped, attempted,
                          day, attempted, client_id, server_id, confirmed, reason, 1, 1))
            conn.execute("INSERT INTO request_events(request_id,event_type,created_at,details_json) VALUES(?,?,?,?)",
                         (request_id, 'LEGACY_IMPORTED', now, json.dumps(list(row))))
            if status == 'blocked_crossover':
                # This row is a guard, not another transmitted message.
                conn.execute("UPDATE send_requests SET transmitted=0, status='failed_pretransmit' WHERE request_id=?", (request_id,))
                conn.execute("INSERT INTO crossover_guards VALUES(?,?,?,?)", (uid, cid, day, request_id))

        # Freeze legacy history; old binaries fail closed instead of bypassing V2.
        for table in ('daily_ledger', 'request_events', 'schema_migrations', 'crossover_guards'):
            for action in ('UPDATE', 'DELETE'):
                conn.execute(f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable ledger history'); END")
        conn.execute("CREATE TRIGGER legacy_insert BEFORE INSERT ON daily_ledger BEGIN SELECT RAISE(ABORT, 'Legacy ledger is read-only; use V2'); END")
        conn.execute("""CREATE TRIGGER request_no_delete BEFORE DELETE ON send_requests
            BEGIN SELECT RAISE(ABORT, 'Immutable request history'); END""")
        conn.execute("""CREATE TRIGGER request_terminal BEFORE UPDATE ON send_requests
            WHEN OLD.status IN ('confirmed','failed_unknown','failed_pretransmit')
            BEGIN SELECT RAISE(ABORT, 'Terminal request is immutable'); END""")
        conn.execute("""CREATE TRIGGER request_identity BEFORE UPDATE ON send_requests
            WHEN NEW.request_id IS NOT OLD.request_id OR NEW.canonical_uid IS NOT OLD.canonical_uid
            OR NEW.conv_id IS NOT OLD.conv_id OR NEW.idempotency_key IS NOT OLD.idempotency_key
            OR NEW.payload_json IS NOT OLD.payload_json OR NEW.client_msg_id IS NOT OLD.client_msg_id
            OR NEW.created_at IS NOT OLD.created_at OR NEW.legacy IS NOT OLD.legacy
            OR NEW.transmitted < OLD.transmitted
            BEGIN SELECT RAISE(ABORT, 'Request identity/quota is immutable'); END""")
        conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (2, now, backup_path))
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def timestamp_ms(value):
    """Parse an explicit timezone; never infer one for historical data."""
    if not isinstance(value, str) or not value:
        raise ValueError('missing attempt_time')
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError('attempt_time requires a timezone')
        delta = instant.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        if micros < 0:
            raise ValueError('attempt_time precedes epoch')
        return (micros + 999) // 1000  # Round up conservatively for legacy sub-ms times.
    except (ValueError, OverflowError) as exc:
        raise ValueError('invalid attempt_time or missing timezone') from exc


def _inspect(conn):
    """Inspect a single snapshot. No DDL, pragmas that write, or data repairs."""
    import ledger
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    report = {'version': version, 'target_version': VERSION, 'request_count': 0,
              'transmitted_count': 0, 'errors': []}
    if version not in (2, 3, 4, VERSION):
        report['errors'].append({'reason': 'Only schema V2, V3, V4 or V5 is supported; no implicit legacy conversion'})
        return report
    try:
        _validate_common_schema(conn)
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Database integrity/foreign key validation failed')
        rows = conn.execute('SELECT request_id,attempt_time,date,transmitted FROM send_requests').fetchall()
        report['request_count'] = len(rows)
        report['unknown_count'] = conn.execute("SELECT count(*) FROM send_requests WHERE status='failed_unknown'").fetchone()[0]
        report['pending_count'] = conn.execute("SELECT count(*) FROM send_requests WHERE status='pending'").fetchone()[0]
        for request_id, attempted, day, transmitted in rows:
            if not transmitted:
                continue
            report['transmitted_count'] += 1
            try:
                timestamp_ms(attempted)
                original = datetime.fromisoformat(attempted.replace('Z', '+00:00'))
                if original.astimezone(ledger.TZ_HO_CHI_MINH).date().isoformat() != day:
                    raise ValueError('date differs from attempt_time in Asia/Ho_Chi_Minh')
            except (ValueError, TypeError) as exc:
                report['errors'].append({'request_id': request_id, 'reason': str(exc)})
        if version in (3, 4, 5):
            validate_schema(conn, expected=version)
    except (sqlite3.DatabaseError, ValueError) as exc:
        report['errors'].append({'reason': str(exc)})
    return report


def preflight(db_path):
    import ledger
    conn = ledger._get_readonly_connection(db_path)
    if conn is None:
        return {'version': None, 'target_version': VERSION, 'request_count': 0,
                'transmitted_count': 0, 'errors': [{'reason': 'Database does not exist'}]}
    with closing(conn):
        conn.execute('BEGIN')
        return _inspect(conn)


def _validate_common_schema(conn):
    required = {'send_requests', 'request_events', 'schema_migrations', 'daily_ledger', 'crossover_guards',
                'single_transmitter', 'request_client_identity', 'request_quota',
                'request_terminal', 'request_identity', 'request_no_delete', 'legacy_insert'}
    for table in ('daily_ledger', 'request_events', 'schema_migrations', 'crossover_guards'):
        for operation in ('UPDATE', 'DELETE'):
            required.add(f'immutable_{table}_{operation}')
    objects = {r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
    if not required <= objects:
        raise ValueError('Ledger schema is incomplete')
    conn.execute('SELECT request_id,canonical_uid,conv_id,idempotency_key,payload_json,status,created_at,date,attempt_time,client_msg_id,server_msg_id,confirmed_time,reason_code,transmitted,legacy FROM send_requests LIMIT 0')
    # The key constraint is SQLite's automatic unique index (not a named index).
    unique_columns = []
    for index in conn.execute('PRAGMA index_list(send_requests)'):
        if index[2] and not index[4]:
            escaped_name = index[1].replace('"', '""')
            unique_columns.append([r[2] for r in conn.execute(f'PRAGMA index_info("{escaped_name}")')])
    if ['canonical_uid', 'idempotency_key'] not in unique_columns:
        raise ValueError('Missing idempotency unique constraint')


def validate_schema(conn, expected=VERSION):
    """Fail closed even if user_version is correct but required structures are missing."""
    if conn.execute('PRAGMA user_version').fetchone()[0] != expected:
        raise ValueError('Ledger requires schema V5; run ledger-preflight and explicit ledger-migrate')
    _validate_common_schema(conn)
    if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (expected,)).fetchone():
        raise ValueError('Missing migration record')
    required = {'request_account_day', 'request_account_time', 'request_marker_insert',
                'request_marker_valid', 'request_marker_immutable'}
    objects = {r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
    if not required <= objects:
        raise ValueError('Ledger schema is incomplete')
    conn.execute('SELECT transmission_started_at_ms FROM send_requests LIMIT 0')
    if conn.execute("SELECT 1 FROM send_requests WHERE transmitted=1 AND (typeof(transmission_started_at_ms)!='integer' OR transmission_started_at_ms<0) LIMIT 1").fetchone():
        raise ValueError('Invalid transmission timestamp; Owner review required')
    if expected >= 4:
        required = {'rotation_state', 'rotation_events', 'rotation_active', 'rotation_identity',
                    'rotation_metadata_insert', 'rotation_cancel_valid',
                    'immutable_rotation_events_UPDATE', 'immutable_rotation_events_DELETE'}
        if not required <= objects:
            raise ValueError('Incomplete rotation schema')
        conn.execute('SELECT campaign_id,rotation_seq,selection_generation,snapshot_json,intent_json FROM send_requests LIMIT 0')

    if expected >= 5:
        required = {'breaker_state', 'breaker_events', 'breaker_scope_history',
                    'immutable_breaker_events_UPDATE', 'immutable_breaker_events_DELETE'}
        if not required <= objects:
            raise ValueError('Incomplete breaker schema')
        conn.execute('SELECT scope_type,scope_key,state,version,category,opened_at_ms,updated_at_ms,retry_not_before_ms,source_request_id FROM breaker_state LIMIT 0')


def _backup(conn, db_path):
    """Caller holds BEGIN IMMEDIATE. Separate reader includes the committed WAL."""
    path = str(Path(db_path).absolute()) + f'.pre-v{conn.execute("PRAGMA user_version").fetchone()[0]+1}-{uuid.uuid4().hex}.db'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(Path(db_path).absolute().as_uri() + '?mode=ro', uri=True)) as source, \
            closing(sqlite3.connect(path)) as dest:
        source.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity check failed')
    return path


def _apply_v3(conn, backup_path):
    terminal_trigger = conn.execute("SELECT sql FROM sqlite_master WHERE name='request_terminal'").fetchone()[0]
    conn.execute('ALTER TABLE send_requests ADD COLUMN transmission_started_at_ms INTEGER')
    conn.execute('DROP TRIGGER request_terminal')
    for request_id, attempted in conn.execute('SELECT request_id,attempt_time FROM send_requests WHERE transmitted=1').fetchall():
        conn.execute('UPDATE send_requests SET transmission_started_at_ms=? WHERE request_id=?', (timestamp_ms(attempted), request_id))
    conn.execute(terminal_trigger)
    conn.execute('CREATE INDEX request_account_day ON send_requests(canonical_uid,date) WHERE transmitted=1')
    conn.execute('CREATE INDEX request_account_time ON send_requests(canonical_uid,transmission_started_at_ms) WHERE transmitted=1')
    conn.execute("""CREATE TRIGGER request_marker_immutable BEFORE UPDATE ON send_requests
        WHEN OLD.transmitted=1 AND (NEW.transmission_started_at_ms IS NOT OLD.transmission_started_at_ms
        OR NEW.date IS NOT OLD.date OR NEW.attempt_time IS NOT OLD.attempt_time)
        BEGIN SELECT RAISE(ABORT,'Transmission marker is immutable'); END""")
    for operation in ('INSERT', 'UPDATE'):
        name = 'request_marker_valid' if operation == 'UPDATE' else 'request_marker_insert'
        conn.execute(f"""CREATE TRIGGER {name} BEFORE {operation} ON send_requests
            WHEN (NEW.transmitted=1 AND (typeof(NEW.transmission_started_at_ms)!='integer'
                  OR NEW.transmission_started_at_ms<0 OR NEW.date IS NULL OR NEW.attempt_time IS NULL))
            OR (NEW.transmitted=0 AND NEW.transmission_started_at_ms IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'Invalid transmission marker'); END""")
    conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (3, datetime.now(timezone.utc).isoformat(), backup_path))
    conn.execute('PRAGMA user_version=3')
    validate_schema(conn, expected=3)


def initialize(db_path):
    """Explicit creation only. Never replace an existing file, even an empty one."""
    import ledger
    path = Path(db_path).absolute()
    if os.path.lexists(path):
        raise FileExistsError('Initialization refuses to replace an existing file')
    ledger._secure_state_dir(str(path.parent))
    with ledger.RunLock(str(path.parent / 'run.lock')):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            ledger._init_db(conn)
            _migrate_v2(conn, str(path))
            conn.execute('BEGIN IMMEDIATE')
            try:
                _apply_v3(conn, None)
                _apply_v4(conn, None)
                _apply_v5(conn, None)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise


def _apply_v4(conn, backup_path):
    """Rebuild with FK enforcement disabled outside the transaction, then check all FKs."""
    validate_schema(conn, expected=3)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='send_requests'").fetchone()[0]
    objects = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='send_requests' AND sql IS NOT NULL AND type IN ('index','trigger')").fetchall()
    ddl = ddl.replace('CREATE TABLE send_requests', 'CREATE TABLE send_requests_v4', 1)
    ddl = ddl.replace("'failed_pretransmit'))", "'failed_pretransmit','cancelled'))")
    conn.execute(ddl)
    conn.execute('INSERT INTO send_requests_v4 SELECT * FROM send_requests')
    for kind, name, sql in objects:
        conn.execute(f'DROP {kind} "{name}"')
    conn.execute('DROP TABLE send_requests')
    conn.execute('ALTER TABLE send_requests_v4 RENAME TO send_requests')
    for kind, name, sql in objects:
        if name == 'request_terminal':
            sql = sql.replace("'failed_pretransmit')", "'failed_pretransmit','cancelled')")
        conn.execute(sql)
    for name, typ in [('campaign_id', 'TEXT'), ('rotation_seq', 'INTEGER'),
                      ('selection_generation', 'INTEGER'), ('snapshot_json', 'TEXT'), ('intent_json', 'TEXT')]:
        conn.execute(f'ALTER TABLE send_requests ADD COLUMN {name} {typ}')
    conn.execute('''CREATE TRIGGER rotation_identity BEFORE UPDATE ON send_requests
        WHEN NEW.campaign_id IS NOT OLD.campaign_id OR NEW.rotation_seq IS NOT OLD.rotation_seq
        OR NEW.selection_generation IS NOT OLD.selection_generation OR NEW.snapshot_json IS NOT OLD.snapshot_json
        OR NEW.intent_json IS NOT OLD.intent_json
        BEGIN SELECT RAISE(ABORT,'Immutable rotation snapshot'); END''')
    conn.execute('''CREATE TRIGGER rotation_metadata_insert BEFORE INSERT ON send_requests
        WHEN (NEW.campaign_id IS NULL AND (NEW.rotation_seq IS NOT NULL OR NEW.selection_generation IS NOT NULL
              OR NEW.snapshot_json IS NOT NULL OR NEW.intent_json IS NOT NULL))
        OR (NEW.campaign_id IS NOT NULL AND (typeof(NEW.rotation_seq)!='integer' OR NEW.rotation_seq<0
              OR typeof(NEW.selection_generation)!='integer' OR NEW.selection_generation<0
              OR NEW.snapshot_json IS NULL OR NEW.intent_json IS NULL))
        BEGIN SELECT RAISE(ABORT,'Incomplete rotation metadata'); END''')
    conn.execute('''CREATE TRIGGER rotation_cancel_valid BEFORE UPDATE ON send_requests
        WHEN NEW.status='cancelled' AND (OLD.status!='queued' OR NEW.transmitted!=0
              OR NEW.transmission_started_at_ms IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'Only untransmitted queued requests can be cancelled'); END''')
    conn.execute("CREATE UNIQUE INDEX rotation_active ON send_requests(canonical_uid,conv_id,campaign_id) WHERE campaign_id IS NOT NULL AND status IN ('queued','pending')")
    conn.execute('''CREATE TABLE rotation_state(canonical_uid TEXT NOT NULL, conv_id TEXT NOT NULL,
        campaign_id TEXT NOT NULL, success_seq INTEGER NOT NULL DEFAULT 0 CHECK(success_seq>=0),
        selection_generation INTEGER NOT NULL DEFAULT 0 CHECK(selection_generation>=0),
        last_success_request_id TEXT REFERENCES send_requests(request_id),
        PRIMARY KEY(canonical_uid,conv_id,campaign_id))''')
    conn.execute('''CREATE TABLE rotation_events(event_id INTEGER PRIMARY KEY,
        canonical_uid TEXT NOT NULL, conv_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
        event_type TEXT NOT NULL, created_at TEXT NOT NULL, details_json TEXT NOT NULL)''')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f"CREATE TRIGGER immutable_rotation_events_{action} BEFORE {action} ON rotation_events BEGIN SELECT RAISE(ABORT,'Immutable rotation history'); END")
    conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (4, datetime.now(timezone.utc).isoformat(), backup_path))
    conn.execute('PRAGMA user_version=4')
    validate_schema(conn, expected=4)
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('V4 foreign key validation failed')


def migrate(db_path):
    """Explicit V2/V3->V4 administration. Recheck preflight under the writer lock."""
    import ledger
    report = preflight(db_path)
    if report['errors']:
        raise ValueError('Migration blocked by preflight; no data changes applied')
    path = Path(db_path).absolute()
    with ledger.RunLock(str(path.parent / 'run.lock')), \
            closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=30)) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        # Rebuild parent table without rewriting child foreign keys; validate before commit.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('BEGIN IMMEDIATE')
        try:
            report = _inspect(conn)
            if report['errors']:
                raise ValueError('Migration blocked by locked preflight')
            if report['version'] == VERSION:
                conn.commit()
                return None
            backup_path = _backup(conn, str(path))
            if report['version'] == 2:
                _apply_v3(conn, backup_path)
            if report['version'] < 4:
                _apply_v4(conn, backup_path)
            _apply_v5(conn, backup_path)
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Post-migration validation failed')
            conn.commit()
            return backup_path
        except BaseException:
            conn.rollback()
            raise


def _apply_v5(conn, backup_path):
    import circuit_breaker as breaker
    validate_schema(conn, expected=4)
    conn.execute("""CREATE TABLE breaker_state(
        scope_type TEXT NOT NULL CHECK(scope_type IN ('account','session')),
        scope_key TEXT NOT NULL CHECK(length(scope_key)>0),
        state TEXT NOT NULL CHECK(state IN ('CLOSED','OPEN')),
        version INTEGER NOT NULL CHECK(version>0), category TEXT NOT NULL,
        opened_at_ms INTEGER NOT NULL CHECK(opened_at_ms>=0),
        updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=opened_at_ms),
        retry_not_before_ms INTEGER, source_request_id TEXT REFERENCES send_requests(request_id),
        canonical_uid TEXT,
        PRIMARY KEY(scope_type,scope_key))""")
    conn.execute("""CREATE TABLE breaker_events(event_id INTEGER PRIMARY KEY,
        scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, version INTEGER NOT NULL,
        event_type TEXT NOT NULL, at_ms INTEGER NOT NULL, evidence_json TEXT NOT NULL,
        reason TEXT NOT NULL, source_request_id TEXT REFERENCES send_requests(request_id),
        FOREIGN KEY(scope_type,scope_key) REFERENCES breaker_state(scope_type,scope_key))""")
    conn.execute('CREATE INDEX breaker_scope_history ON breaker_events(scope_type,scope_key,event_id)')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f"CREATE TRIGGER immutable_breaker_events_{action} BEFORE {action} ON breaker_events BEGIN SELECT RAISE(ABORT,'Immutable breaker history'); END")
    instant = breaker.now_ms()
    for request_id, uid in conn.execute("SELECT request_id,canonical_uid FROM send_requests WHERE status='failed_unknown' ORDER BY request_id").fetchall():
        breaker.trip(conn, 'account', uid, breaker.Failure('UNKNOWN', 'migration', 'ledger'),
                     request_id, event_type='LEGACY_UNKNOWN_IMPORTED', at_ms=instant)
    conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (5, datetime.now(timezone.utc).isoformat(), backup_path))
    conn.execute('PRAGMA user_version=5')
    validate_schema(conn)
