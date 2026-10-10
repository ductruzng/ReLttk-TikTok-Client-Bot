"""3A offline limits, schema administration and process-contention regressions."""
import asyncio
from contextlib import closing, redirect_stdout
from datetime import datetime
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import ledger
import ledger_migrations as migrations
import ledger_requests as requests
import main
import oneshot
import services


def epoch(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


BASE = epoch('2026-10-09T12:00:00+07:00')


def contend(path, index, barrier, output, settings):
    # Each contender has a separate interpreter and SQLite connection.
    with patch.object(requests, 'utc_now_ms', return_value=BASE):
        row, _ = requests.create_request('u', f'new-{index}', f'race-{index}', {'message': 'hello'}, db_path=path)
        barrier.wait(timeout=15)
        try:
            requests.start_transmission(row['request_id'], db_path=path, **settings)
            requests.finish('u', row['conv_id'], '2026-10-09', row['client_msg_id'], db_path=path,
                            status='confirmed', reason='CONFIRMED_ECHO', server_id='123')
            output.put('admitted')
        except requests.RateLimitError as exc:
            output.put([r['code'] for r in exc.reasons])


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'fixture.db')
        migrations.initialize(self.path)
        self.clock = BASE
        mocked = patch.object(requests, 'utc_now_ms', side_effect=lambda: self.clock)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.sequence = 0

    def request(self, uid='u', cid=None, key=None):
        self.sequence += 1
        return requests.create_request(uid, cid or f'c-{self.sequence}', key or f'key-{self.sequence}',
                                       {'message': 'hello'}, db_path=self.path)[0]

    def start(self, row, **settings):
        return requests.start_transmission(row['request_id'], db_path=self.path, **settings)

    def finish(self, row, status='confirmed'):
        stored = requests.get_request(row['request_id'], db_path=self.path)
        requests.finish(row['canonical_uid'], row['conv_id'], stored['date'], row['client_msg_id'],
                        db_path=self.path, status=status, reason='CONFIRMED_ECHO' if status == 'confirmed' else 'TIMEOUT',
                        server_id='123' if status == 'confirmed' else None)

    def send(self, uid='u', cid=None, **settings):
        row = self.request(uid=uid, cid=cid)
        self.assertTrue(self.start(row, **settings))
        self.finish(row)
        return row

    def blocked(self, row, code, **settings):
        with self.assertRaises(requests.RateLimitError) as caught:
            self.start(row, **settings)
        self.assertIn(code, [r['code'] for r in caught.exception.reasons])
        self.assertEqual(requests.get_request(row['request_id'], db_path=self.path)['status'], 'queued')
        return caught.exception

    def test_all_settings_are_configurable_and_validated(self):
        self.assertEqual(requests.settings_from_config({}), requests.DEFAULT_SETTINGS)
        for name in requests.DEFAULT_SETTINGS:
            bad = ['false', 1, None] if name == 'allow_repeat_same_day' else [True, 1.2, '20', None, -1, 2**40]
            if name != 'min_send_interval_seconds' and name != 'allow_repeat_same_day':
                bad.append(0)
            for value in bad:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    requests.settings_from_config({name: value})
        settings = requests.settings_from_config(dict(max_sends_per_account_per_day=30,
            max_sends_per_conversation_per_day=3, allow_repeat_same_day=True,
            min_send_interval_seconds=0, max_sends_per_window=7, window_seconds=20))
        plan = Path(self.tmp.name) / 'plan.json'
        services.save_send_settings(settings.pop('allow_repeat_same_day'),
            settings.pop('max_sends_per_conversation_per_day'), path=plan, **settings)
        self.assertEqual(services.read_json_object(plan)['window_seconds'], 20)

    def test_account_daily_limit_is_shared_across_conversations_not_accounts(self):
        for _ in range(2):
            self.send(max_sends_per_account_per_day=2)
            self.clock += 60000
        exc = self.blocked(self.request(), 'ACCOUNT_DAILY_LIMIT', max_sends_per_account_per_day=2)
        self.assertIn('2/2', str(exc))
        self.assertEqual(exc.retry_at, '2026-10-10T00:00:00+07:00')
        self.send(uid='other-account', max_sends_per_account_per_day=2)

    def test_repeat_requires_both_permission_and_larger_conversation_quota(self):
        self.send(cid='same')
        self.clock += 60000
        row = self.request(cid='same')
        self.blocked(row, 'CONVERSATION_DAILY_LIMIT', allow_repeat_same_day=True)
        self.blocked(row, 'REPEAT_DISABLED', max_sends_per_conversation_per_day=2)
        self.assertTrue(self.start(row, allow_repeat_same_day=True, max_sends_per_conversation_per_day=2))

    def test_interval_before_at_and_after_boundary(self):
        self.send()
        row = self.request()
        self.clock = BASE + 59999
        exc = self.blocked(row, 'MIN_INTERVAL')
        self.assertEqual(exc.reasons[0]['retry_at_ms'], BASE + 60000)
        self.clock += 1
        self.assertTrue(self.start(row))
        self.finish(row)
        self.clock += 60001
        self.send()

    def test_sliding_window_exact_open_left_boundary_and_identical_timestamps(self):
        settings = dict(min_send_interval_seconds=0, max_sends_per_window=2, window_seconds=300)
        self.send(**settings)
        self.send(**settings)
        row = self.request()
        self.clock = BASE + 299999
        exc = self.blocked(row, 'SLIDING_WINDOW', **settings)
        self.assertEqual(exc.retry_at, requests.local_time(BASE + 300000).isoformat())
        self.clock += 1
        self.assertTrue(self.start(row, **settings))
        self.finish(row)
        self.clock += 1
        self.send(**settings)

    def test_reducing_window_limit_uses_nth_latest_expiration(self):
        settings = dict(min_send_interval_seconds=0, max_sends_per_window=5, window_seconds=300)
        for offset in (0, 10000, 20000, 30000):
            self.clock = BASE + offset
            self.send(**settings)
        self.clock = BASE + 40000
        row = self.request()
        exc = self.blocked(row, 'SLIDING_WINDOW', min_send_interval_seconds=0, max_sends_per_window=2)
        self.assertEqual(exc.reasons[0]['retry_at_ms'], BASE + 320000)
        self.assertTrue(self.start(row, min_send_interval_seconds=0, max_sends_per_window=5))

    def test_midnight_resets_daily_only_not_interval_or_window(self):
        self.clock = epoch('2026-10-09T23:59:59+07:00')
        self.send(max_sends_per_account_per_day=1)
        self.clock += 1000
        row = self.request()
        exc = self.blocked(row, 'MIN_INTERVAL', max_sends_per_account_per_day=1, max_sends_per_window=1)
        self.assertEqual({r['code'] for r in exc.reasons}, {'MIN_INTERVAL', 'SLIDING_WINDOW'})
        self.clock += 299000
        self.assertTrue(self.start(row, max_sends_per_account_per_day=1, max_sends_per_window=1))
        self.assertEqual(requests.get_request(row['request_id'], db_path=self.path)['date'], '2026-10-10')

    def test_all_reasons_and_latest_retry_are_returned(self):
        self.send(cid='same')
        row = self.request(cid='same')
        exc = self.blocked(row, 'REPEAT_DISABLED', max_sends_per_account_per_day=1, max_sends_per_window=1)
        self.assertEqual({r['code'] for r in exc.reasons}, {'REPEAT_DISABLED', 'ACCOUNT_DAILY_LIMIT',
            'CONVERSATION_DAILY_LIMIT', 'MIN_INTERVAL', 'SLIDING_WINDOW'})
        self.assertEqual(exc.retry_at, '2026-10-10T00:00:00+07:00')

    def test_unknown_consumes_all_limits_and_has_no_guaranteed_retry_time(self):
        first = self.request(cid='same')
        self.start(first)
        self.finish(first, status='failed_unknown')
        exc = self.blocked(self.request(cid='same'), 'UNKNOWN', max_sends_per_account_per_day=1, max_sends_per_window=1)
        self.assertIsNone(exc.retry_at)
        self.assertIn('ACCOUNT_DAILY_LIMIT', [r['code'] for r in exc.reasons])
        self.clock += 86400000
        self.blocked(self.request(cid='same'), 'UNKNOWN', allow_repeat_same_day=True, max_sends_per_conversation_per_day=100)

    def test_pretransmit_failure_and_key_replay_do_not_consume_more(self):
        failed = self.request()
        requests.fail_pretransmit(failed['request_id'], db_path=self.path)
        first = self.send(max_sends_per_account_per_day=1)
        replay, created = requests.create_request('u', first['conv_id'], first['idempotency_key'],
                                                  {'message': 'hello'}, db_path=self.path)
        self.assertFalse(created)
        self.assertFalse(self.start(replay, max_sends_per_account_per_day=1))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT sum(transmitted) FROM send_requests').fetchone()[0], 1)

    def test_restart_and_recovery_retain_all_rate_limit_markers(self):
        row = self.request()
        self.start(row)
        with ledger.RunLock(str(Path(self.tmp.name) / 'run.lock')) as lock:
            requests.recover_pending(db_path=self.path, run_lock=lock)
        self.blocked(self.request(), 'MIN_INTERVAL')
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT transmission_started_at_ms FROM send_requests WHERE transmitted=1').fetchone()[0], BASE)

    def test_claim_event_failure_rolls_back_timestamp_and_quota(self):
        row = self.request()
        with patch.object(requests, 'event', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError): self.start(row)
        stored = requests.get_request(row['request_id'], db_path=self.path)
        self.assertEqual((stored['status'], stored['transmitted'], stored['transmission_started_at_ms']), ('queued', 0, None))
        self.assertTrue(self.start(row))

    def test_clock_rollback_is_fail_safe_across_restart(self):
        self.send()
        self.clock -= 1
        exc = self.blocked(self.request(), 'CLOCK_SKEW', min_send_interval_seconds=0)
        self.assertIsNone(exc.retry_at)
        self.clock = BASE + 60000
        self.send()

    def test_explicit_date_cannot_override_clock_for_daily_quota(self):
        row = self.request()
        with self.assertRaises(ValueError): self.start(row, date='2026-10-10')
        self.assertEqual(requests.get_request(row['request_id'], db_path=self.path)['transmitted'], 0)

    def test_two_processes_compete_for_last_daily_slot(self):
        self._race(dict(min_send_interval_seconds=0, max_sends_per_account_per_day=1))

    def test_two_processes_compete_for_last_window_slot(self):
        self._race(dict(min_send_interval_seconds=0, max_sends_per_window=1))

    def test_two_processes_cannot_bypass_interval(self):
        self._race(dict(min_send_interval_seconds=60))

    def _race(self, settings):
        ctx = multiprocessing.get_context('spawn')
        barrier, output = ctx.Barrier(2), ctx.Queue()
        children = [ctx.Process(target=contend, args=(self.path, i, barrier, output, settings)) for i in (1, 2)]
        try:
            for child in children: child.start()
            results = [output.get(timeout=20) for _ in children]
            for child in children:
                child.join(timeout=20)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(results.count('admitted'), 1)
            rejected = next(result for result in results if result != 'admitted')
            expected = ('ACCOUNT_DAILY_LIMIT' if settings.get('max_sends_per_account_per_day') == 1 else
                        'SLIDING_WINDOW' if settings.get('max_sends_per_window') == 1 else 'MIN_INTERVAL')
            self.assertIn(expected, rejected)
            with closing(sqlite3.connect(self.path)) as conn:
                self.assertEqual(conn.execute('SELECT sum(transmitted) FROM send_requests').fetchone()[0], 1)
        finally:
            for child in children:
                if child.is_alive(): child.terminate()
                child.join(timeout=5)
            output.close()


class MigrationV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'fixture.db')

    def v2(self, attempted='2026-10-09T12:00:00+07:00', day='2026-10-09', status='failed_unknown'):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        migrations._migrate_v2(conn, self.path)
        conn.execute('''INSERT INTO send_requests(request_id,canonical_uid,conv_id,idempotency_key,payload_json,
            status,created_at,date,attempt_time,client_msg_id,transmitted)
            VALUES('request','u','c','key','{}',?,'created',?,?,'client',1)''', (status, day, attempted))
        conn.execute("INSERT INTO request_events(request_id,event_type,created_at,details_json) VALUES('request','IMPORT','time','{}')")
        return conn

    def test_wal_backup_preserves_v2_and_restore_and_repeat_are_safe(self):
        with closing(self.v2()) as source:
            before = source.execute('SELECT * FROM send_requests').fetchall()
            events = source.execute('SELECT * FROM request_events').fetchall()
            self.assertGreater(Path(self.path + '-wal').stat().st_size, 0)
            report = migrations.preflight(self.path)
            self.assertEqual(report['errors'], [])
            self.assertEqual((report['request_count'], report['transmitted_count']), (1, 1))
            backup = migrations.migrate(self.path)
            self.assertIsNone(migrations.migrate(self.path))
            self.assertEqual(len(list(Path(self.tmp.name).glob('*.pre-v3-*.db'))), 1)
            after = source.execute('SELECT * FROM send_requests').fetchall()
            self.assertEqual([r[:15] for r in after], before)
            self.assertEqual(after[0][15], BASE)
            self.assertEqual(source.execute('SELECT * FROM request_events').fetchall(), events)
            self.assertEqual(source.execute('SELECT count(*) FROM daily_ledger').fetchone()[0], 0)
            with closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(saved.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertEqual(saved.execute('SELECT * FROM send_requests').fetchall(), before)
                restored = str(Path(self.tmp.name) / 'restored.db')
                with closing(sqlite3.connect(restored)) as dest:
                    saved.backup(dest)
                    self.assertEqual(dest.execute('SELECT * FROM send_requests').fetchall(), before)
            with self.assertRaises(sqlite3.IntegrityError):
                source.execute("UPDATE send_requests SET status='confirmed'")
            with self.assertRaises(sqlite3.IntegrityError):
                source.execute('DELETE FROM request_events')

    def test_bad_timestamp_blocks_preflight_and_apply_without_data_changes(self):
        for value in (None, '', 'bad', '2026-10-09T12:00:00', '1960-01-01T00:00:00Z'):
            with self.subTest(value=value):
                self.path = str(Path(self.tmp.name) / f'case-{len(list(Path(self.tmp.name).glob("*.db")))}.db')
                with closing(self.v2(attempted=value)) as conn:
                    before = list(conn.iterdump())
                    report = migrations.preflight(self.path)
                    self.assertEqual(report['transmitted_count'], 1)
                    self.assertTrue(report['errors'])
                    with self.assertRaises(ValueError): migrations.migrate(self.path)
                    self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(list(Path(self.tmp.name).glob('*.pre-v3-*.db')), [])

    def test_day_mismatch_is_not_repaired_or_guessed(self):
        with closing(self.v2(day='2026-10-08')):
            self.assertIn('date differs', migrations.preflight(self.path)['errors'][0]['reason'])
            with self.assertRaises(ValueError): migrations.migrate(self.path)

    def test_full_legacy_history_and_all_request_states_survive_v3(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.execute('''CREATE TABLE daily_ledger(canonical_uid TEXT,conv_id TEXT,date TEXT,status TEXT,
                attempt_time TEXT,client_msg_id TEXT,server_msg_id TEXT,confirmed_time TEXT,reason_code TEXT,
                PRIMARY KEY(canonical_uid,conv_id,date))''')
            for i, status in enumerate(('confirmed', 'pending', 'failed_unknown', 'blocked_crossover')):
                conn.execute('INSERT INTO daily_ledger VALUES(?,?,?,?,?,?,?,?,?)',
                    ('u', f'c-{i}', '2026-10-09', status, '2026-10-09T12:00:00+07:00', f'client-{i}', None, None, 'UNKNOWN'))
            migrations._migrate_v2(conn, self.path)
            for status, transmitted in (('queued', 0), ('failed_pretransmit', 0), ('pending', 1)):
                conn.execute('''INSERT INTO send_requests(request_id,canonical_uid,conv_id,idempotency_key,payload_json,
                    status,created_at,date,attempt_time,client_msg_id,transmitted) VALUES(?, 'u', ?, ?, '{}', ?, 'created', ?, ?, ?, ?)''',
                    (status, status, status, status, '2026-10-09' if transmitted else None,
                     '2026-10-09T12:00:00+07:00' if transmitted else None, status, transmitted))
            legacy = conn.execute('SELECT * FROM daily_ledger').fetchall()
            history = conn.execute('SELECT * FROM request_events').fetchall()
            requests_before = conn.execute('SELECT * FROM send_requests').fetchall()
            migrations.migrate(self.path)
            self.assertEqual(conn.execute('SELECT * FROM daily_ledger').fetchall(), legacy)
            self.assertEqual(conn.execute('SELECT * FROM request_events').fetchall(), history)
            after = conn.execute('SELECT * FROM send_requests').fetchall()
            self.assertEqual([r[:15] for r in after], requests_before)
            for row in after:
                self.assertEqual(row[15], BASE if row[13] else None)

    def test_readonly_preflight_does_not_call_backup_or_write_statements(self):
        with closing(self.v2()) as conn:
            before = list(conn.iterdump())
            with patch.object(migrations, '_backup', side_effect=AssertionError('must not back up')):
                self.assertEqual(migrations.preflight(self.path)['errors'], [])
            self.assertEqual(list(conn.iterdump()), before)
            self.assertEqual(list(Path(self.tmp.name).glob('*.pre-v3-*.db')), [])

    def test_missing_history_trigger_is_rejected_in_preflight(self):
        with closing(self.v2()) as conn:
            conn.execute('DROP TRIGGER immutable_request_events_DELETE')
            self.assertTrue(migrations.preflight(self.path)['errors'])
            with self.assertRaises(ValueError): migrations.migrate(self.path)

    def test_timezone_and_submillisecond_conversion_are_conservative(self):
        self.assertEqual(migrations.timestamp_ms('2026-10-09T05:00:00Z'), BASE)
        self.assertEqual(migrations.timestamp_ms('2026-10-09T12:00:00.000001+07:00'), BASE + 1)

    def test_migration_failure_restores_schema_triggers_and_history(self):
        with closing(self.v2()) as conn:
            before = list(conn.iterdump())
            original = migrations._apply_v3
            def fail_after_ddl(*args):
                original(*args)
                raise RuntimeError('injected before commit')
            with patch.object(migrations, '_apply_v3', side_effect=fail_after_ddl):
                with self.assertRaises(RuntimeError): migrations.migrate(self.path)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(list(conn.iterdump()), before)
            self.assertEqual(len(list(Path(self.tmp.name).glob('*.pre-v3-*.db'))), 1)
            migrations.migrate(self.path)
            ledger.check_ready(self.path)

    def test_backup_failure_aborts_before_schema_changes(self):
        with closing(self.v2()) as conn:
            before = list(conn.iterdump())
            with patch.object(migrations, '_backup', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): migrations.migrate(self.path)
            self.assertEqual(list(conn.iterdump()), before)

    def test_explicit_initialize_never_overwrites_or_implicitly_migrates(self):
        missing = str(Path(self.tmp.name) / 'missing' / 'db.sqlite')
        with self.assertRaises(ledger.LedgerError): ledger._get_readwrite_connection(missing)
        self.assertFalse(Path(missing).parent.exists())
        self.assertTrue(migrations.preflight(missing)['errors'])
        self.assertFalse(Path(missing).parent.exists())
        migrations.initialize(self.path)
        with self.assertRaises(FileExistsError): migrations.initialize(self.path)
        ledger.check_ready(self.path)

    def test_old_or_incomplete_schema_rejected_by_send_opener_without_migration(self):
        with closing(self.v2()) as conn:
            before = list(conn.iterdump())
            with self.assertRaises(ValueError): ledger._get_readwrite_connection(self.path)
            self.assertEqual(list(conn.iterdump()), before)
            self.assertEqual(list(Path(self.tmp.name).glob('*.pre-v3-*.db')), [])
            migrations.migrate(self.path)
            conn.execute('DROP INDEX single_transmitter')
            with self.assertRaises(ValueError): ledger.check_ready(self.path)


class RateLimitCLITests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_gate_precedes_session_and_network_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            for version in (None, 2, 99):
                path = str(Path(tmp) / f'{version}.db')
                if version is not None:
                    with closing(sqlite3.connect(path)) as conn:
                        conn.execute(f'PRAGMA user_version={version}')
                with patch.object(oneshot, 'load_session') as session, patch.object(oneshot, '_open_ws', AsyncMock()) as network:
                    with self.assertRaises((ledger.LedgerError, ValueError)):
                        await oneshot.run_oneshot_send('nonexistent-plan.json', db_path=path)
                    session.assert_not_called()
                    network.assert_not_called()
                if version is None: self.assertFalse(Path(path).exists())

    async def test_rate_block_is_visible_and_does_not_call_websocket_send(self):
        from test_streak import MockWebSocket
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'fixture.db')
            migrations.initialize(path)
            with patch.object(requests, 'utc_now_ms', return_value=BASE):
                first, _ = requests.create_request('u', 'other', 'first', {}, db_path=path)
                requests.start_transmission(first['request_id'], db_path=path)
                requests.finish('u', 'other', '2026-10-09', first['client_msg_id'], db_path=path,
                                status='confirmed', reason='CONFIRMED_ECHO', server_id='123')
                ws = MockWebSocket()
                result = await oneshot._send_target_oneshot(ws, 'u', {'conv_id': 'c', 'conv_short_id': 1, 'conv_type': 1},
                    'hello', db_path=path, max_sends_per_account_per_day=1, max_sends_per_window=1)
                self.assertEqual(ws.sent_packets, [])
                self.assertEqual({r['code'] for r in result['blocks']}, {'ACCOUNT_DAILY_LIMIT', 'MIN_INTERVAL', 'SLIDING_WINDOW'})
            text = io.StringIO()
            with patch.object(services, 'send_plan', AsyncMock(return_value={'results': [result]})), redirect_stdout(text):
                # cmd_send owns asyncio.run, so exercise it outside this event loop.
                await asyncio.to_thread(main.cmd_send, 'unused.json')
            self.assertIn('ACCOUNT_DAILY_LIMIT: 1/1', text.getvalue())
            self.assertIn('Re-evaluate at:', text.getvalue())
            self.assertIn('no automatic retry', text.getvalue())

    async def test_admin_preflight_initialize_and_migrate_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'fixture.db')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_ledger_admin('ledger-preflight', path), 1)
                self.assertFalse(Path(path).exists())
                self.assertEqual(main.cmd_ledger_admin('ledger-initialize', path), 0)
                self.assertEqual(main.cmd_ledger_admin('ledger-initialize', path), 1)
                self.assertEqual(main.cmd_ledger_admin('ledger-preflight', path), 0)
                self.assertEqual(main.cmd_ledger_admin('ledger-migrate', path), 0)

    async def test_readonly_status_and_dryrun_never_initialize_or_migrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'missing' / 'ledger.db')
            plan = Path(tmp) / 'plan.json'
            plan.write_text(json.dumps({'session': 'fake', 'message': 'hello',
                'targets': [{'conv_id': 'c', 'conv_short_id': 1, 'conv_type': 1}]}))
            with patch.object(migrations, 'initialize', side_effect=AssertionError('no initialization')), \
                    patch.object(migrations, 'migrate', side_effect=AssertionError('no migration')), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_status(db_path=path), 0)
                self.assertEqual(main.cmd_dry_run(str(plan)), 0)
            self.assertFalse(Path(path).parent.exists())

    async def test_main_parser_requires_explicit_admin_path_and_routes_commands(self):
        import sys
        with patch.object(sys, 'argv', ['main.py', 'ledger-migrate']), redirect_stdout(io.StringIO()):
            from contextlib import redirect_stderr
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main.main()
        with patch.object(sys, 'argv', ['main.py', 'ledger-preflight', '--db', 'fixture-only.db']), \
                patch.object(main, 'cmd_ledger_admin', return_value=0) as admin:
            self.assertEqual(main.main(), 0)
            admin.assert_called_once_with('ledger-preflight', 'fixture-only.db')
