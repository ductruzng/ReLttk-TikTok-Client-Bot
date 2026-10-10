"""Phase 2 fixtures only; no credentials, live databases, or TikTok transport."""
from contextlib import closing
import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ledger
import ledger_migrations
import ledger_requests as requests
from ledger_fixtures import initialize_fixture, start_at, migrate_v2_fixture


def race_worker(path, key):
    row, _ = requests.create_request('u', 'c', key, {'message': 'hello'}, db_path=path)
    try:
        started = start_at(row['request_id'], db_path=path, date='2026-10-09')
    except ledger.QuotaExceededError:
        started = False
    return row['request_id'], started


class LedgerV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'fixture.db')

    def create(self, key='key', message='hello', cid='c'):
        initialize_fixture(self.path)
        return requests.create_request('u', cid, key, {'message': message}, db_path=self.path)[0]

    def start(self, row, **settings):
        return start_at(row['request_id'], db_path=self.path, date='2026-10-09', **settings)

    def confirm(self, row):
        ledger.confirm_send('u', row['conv_id'], '2026-10-09', row['client_msg_id'], '123',
                            confirm_date='2026-10-09', db_path=self.path)

    def query(self, sql):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(sql).fetchall()

    def legacy_fixture(self):
        conn = sqlite3.connect(self.path)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('''CREATE TABLE daily_ledger(canonical_uid TEXT,conv_id TEXT,date TEXT,status TEXT,
            attempt_time TEXT,client_msg_id TEXT,server_msg_id TEXT,confirmed_time TEXT,reason_code TEXT,
            PRIMARY KEY(canonical_uid,conv_id,date))''')
        rows = [('u', 'c' + str(i), '2026-10-08', status, 'time', 'id' + str(i), None, None, 'UNKNOWN')
                for i, status in enumerate(('confirmed', 'pending', 'failed_unknown', 'blocked_crossover'))]
        conn.executemany('INSERT INTO daily_ledger VALUES(?,?,?,?,?,?,?,?,?)', rows)
        conn.commit()
        return conn, rows

    def test_backup_reads_wal_and_migration_preserves_source_and_is_idempotent(self):
        source, rows = self.legacy_fixture()
        try:
            self.assertGreater(os.path.getsize(self.path + '-wal'), 0)
            migrate_v2_fixture(self.path)
            self.assertEqual(self.query('SELECT * FROM daily_ledger'), rows)
            self.assertEqual(self.query('PRAGMA user_version'), [(2,)])
            backup = self.query('SELECT backup_path FROM schema_migrations')[0][0]
            with closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(saved.execute('SELECT * FROM daily_ledger').fetchall(), rows)
                self.assertEqual(saved.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(self.query('SELECT status,transmitted FROM send_requests'),
                             [('confirmed', 1), ('failed_unknown', 1), ('failed_unknown', 1), ('failed_pretransmit', 0)])
            before = self.query('SELECT * FROM send_requests')
            migrate_v2_fixture(self.path)
            self.assertEqual(self.query('SELECT * FROM send_requests'), before)
            self.assertEqual(len(list(Path(self.tmp.name).glob('*.pre-v2-*.db'))), 1)
            for sql in ('DELETE FROM daily_ledger', "UPDATE daily_ledger SET status='confirmed'",
                        'DELETE FROM request_events', 'DELETE FROM send_requests'):
                with self.subTest(sql=sql), closing(sqlite3.connect(self.path)) as conn:
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql)
            # Restore verification on another fixture, never over the active V2 file.
            restored = str(Path(self.tmp.name) / 'restored.db')
            with closing(sqlite3.connect(backup)) as saved, closing(sqlite3.connect(restored)) as dest:
                saved.backup(dest)
                self.assertEqual(dest.execute('SELECT * FROM daily_ledger').fetchall(), rows)
        finally:
            source.close()

    def test_migration_failure_rolls_back_then_can_rerun(self):
        conn, rows = self.legacy_fixture()
        class FaultConnection:
            def execute(self, sql, *args):
                if sql.startswith('CREATE INDEX request_quota'):
                    raise RuntimeError('injected migration crash')
                return conn.execute(sql, *args)
            def commit(self): conn.commit()
            def rollback(self): conn.rollback()
        try:
            with self.assertRaises(RuntimeError):
                ledger_migrations._migrate_v2(FaultConnection(), self.path)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT * FROM daily_ledger').fetchall(), rows)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='send_requests'").fetchone())
            ledger_migrations._migrate_v2(conn, self.path)
            self.assertEqual(conn.execute('SELECT count(*) FROM send_requests').fetchone()[0], 4)
        finally:
            conn.close()

    def test_key_payload_and_repeat_are_independent(self):
        first = self.create()
        self.start(first)
        self.confirm(first)
        replay, created = requests.create_request('u', 'c', 'key', {'message': 'hello'}, db_path=self.path)
        self.assertFalse(created)
        self.assertEqual(replay['request_id'], first['request_id'])
        self.assertFalse(self.start(replay, allow_repeat_same_day=True, max_sends_per_conversation_per_day=9))
        with self.assertRaises(requests.IdempotencyConflict):
            self.create(message='changed')
        second = self.create('new')
        with self.assertRaisesRegex(ledger.QuotaExceededError, '1/1'):
            self.start(second, allow_repeat_same_day=True)
        with self.assertRaisesRegex(ledger.QuotaExceededError, 'allow_repeat_same_day=false'):
            self.start(second, max_sends_per_conversation_per_day=2, min_send_interval_seconds=0)
        self.start(second, allow_repeat_same_day=True, max_sends_per_conversation_per_day=2, min_send_interval_seconds=0)
        self.confirm(second)
        self.assertEqual(self.query('SELECT count(*) FROM send_requests WHERE transmitted=1'), [(2,)])
        self.assertEqual(self.query("SELECT count(*) FROM request_events WHERE event_type='TRANSMISSION_STARTED'"), [(2,)])

    def test_unknown_never_changes_and_blocks_next_day_even_with_higher_limit(self):
        first = self.create()
        self.start(first)
        ledger.mark_failed_unknown('u', 'c', '2026-10-09', first['client_msg_id'], db_path=self.path)
        before = self.query('SELECT * FROM send_requests')
        second = self.create('replacement')
        for day in ('2026-10-09', '2026-10-10'):
            with self.assertRaisesRegex(ledger.QuotaExceededError, 'UNKNOWN'):
                start_at(second['request_id'], db_path=self.path, date=day,
                                            allow_repeat_same_day=True, max_sends_per_conversation_per_day=10)
        self.assertEqual(self.query('SELECT * FROM send_requests')[0], before[0])
        with closing(sqlite3.connect(self.path)) as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE send_requests SET status='confirmed' WHERE status='failed_unknown'")

    def test_pretransmit_failure_does_not_consume_quota_or_retry_same_request(self):
        first = self.create()
        requests.fail_pretransmit(first['request_id'], db_path=self.path)
        self.assertFalse(self.start(first))
        second = self.create('new')
        self.assertTrue(self.start(second))
        self.assertEqual(self.query('SELECT sum(transmitted) FROM send_requests'), [(1,)])

    def test_transaction_rolls_back_transmission_marker_and_event_together(self):
        row = self.create()
        with patch.object(requests, 'event', side_effect=RuntimeError('injected fault')):
            with self.assertRaises(RuntimeError): self.start(row)
        self.assertEqual(self.query('SELECT status,transmitted FROM send_requests'), [('queued', 0)])
        self.assertTrue(self.start(row))

    def test_two_processes_same_key_only_one_request_and_transmission(self):
        initialize_fixture(self.path)
        with concurrent.futures.ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool:
            results = list(pool.map(race_worker, [self.path] * 2, ['same'] * 2))
        self.assertEqual(len({r[0] for r in results}), 1)
        self.assertEqual(sum(r[1] for r in results), 1)
        self.assertEqual(self.query('SELECT count(*) FROM send_requests'), [(1,)])

    def test_two_processes_new_keys_still_share_quota_and_single_transmitter(self):
        initialize_fixture(self.path)
        with concurrent.futures.ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool:
            results = list(pool.map(race_worker, [self.path] * 2, ['one', 'two']))
        self.assertEqual(len({r[0] for r in results}), 2)
        self.assertEqual(sum(r[1] for r in results), 1)

    def test_database_constraints_reject_duplicate_identity_and_second_transmitter(self):
        first = self.create()
        second = self.create('second', cid='different-conversation')
        self.start(first)
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'UNIQUE constraint failed'):
                conn.execute("UPDATE send_requests SET status='pending',transmitted=1,transmission_started_at_ms=1791522000000,date='2026-10-09',attempt_time='2026-10-09T12:00:00+07:00' WHERE request_id=?", (second['request_id'],))
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""INSERT INTO send_requests(request_id,canonical_uid,conv_id,idempotency_key,
                    payload_json,status,created_at,client_msg_id) VALUES('duplicate','u','c','key','{}','queued','now','other-client')""")

    def test_concurrent_migration_backs_up_legacy_only_once(self):
        source, rows = self.legacy_fixture()
        try:
            def migrate(_):
                migrate_v2_fixture(self.path)
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                list(pool.map(migrate, range(4)))
            self.assertEqual(self.query('SELECT * FROM daily_ledger'), rows)
            self.assertEqual(self.query('SELECT count(*) FROM schema_migrations'), [(1,)])
            self.assertEqual(len(list(Path(self.tmp.name).glob('*.pre-v2-*.db'))), 1)
        finally:
            source.close()

    def test_process_death_before_and_after_marker(self):
        for boundary in ('queued', 'uncommitted', 'committed'):
            with self.subTest(boundary=boundary):
                path = str(Path(self.tmp.name) / (boundary + '.db'))
                initialize_fixture(path)
                script = '''import os, sys
import ledger, ledger_requests as r
p, boundary = sys.argv[1:]
row, _ = r.create_request('u','c','key',{'message':'hello'},db_path=p)
if boundary == 'committed':
    r.start_transmission(row['request_id'],db_path=p)
elif boundary == 'uncommitted':
    conn=ledger._get_readwrite_connection(p)
    conn.execute('BEGIN IMMEDIATE')
    conn.execute("UPDATE send_requests SET status='pending',transmitted=1,transmission_started_at_ms=1,date='1970-01-01',attempt_time='1970-01-01T00:00:00.001+00:00'")
os._exit(17)
'''
                result = subprocess.run([sys.executable, '-B', '-c', script, path, boundary],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 17, result.stderr.decode())
                with ledger.RunLock(str(Path(self.tmp.name) / 'run.lock')) as run_lock:
                    count = requests.recover_pending(db_path=path, run_lock=run_lock)
                    self.assertEqual(requests.recover_pending(db_path=path, run_lock=run_lock), 0)
                with closing(sqlite3.connect(path)) as conn:
                    state = conn.execute('SELECT status,transmitted FROM send_requests').fetchone()
                self.assertEqual(state, ('failed_unknown', 1) if boundary == 'committed' else ('queued', 0))
                self.assertEqual(count, int(boundary == 'committed'))
                with self.assertRaises(ledger.LockError):
                    requests.recover_pending(db_path=path, run_lock=run_lock)

    def test_invalid_settings_and_no_destructive_repeat_branch(self):
        for repeat, limit in (('false', 1), (False, True), (True, 0), (True, 1.5)):
            with self.assertRaises(ValueError): requests.validate_settings(repeat, limit)
        root = Path(__file__).resolve().parents[1]
        for name in ('cli.py', 'tiktok_tui.py', 'services.py', 'oneshot.py', 'ledger.py', 'ledger_requests.py'):
            code = (root / name).read_text(encoding='utf-8').upper()
            self.assertNotIn('DELETE FROM', code, name)
            self.assertNotIn('INSERT OR REPLACE', code, name)
        for name in ('cli.py', 'tiktok_tui.py', 'main.py'):
            self.assertIn('send_plan', (root / name).read_text(encoding='utf-8'))

    def test_cli_key_forwarding_and_settings_preserve_plan(self):
        import main
        import services
        with patch.object(sys, 'argv', ['main.py', '--send', '--yes', '--idempotency-key', 'task-123']), \
                patch.object(main, 'cmd_send', return_value=0) as send:
            self.assertEqual(main.main(), 0)
            self.assertEqual(send.call_args.kwargs['idempotency_key'], 'task-123')
        plan = Path(self.tmp.name) / 'plan.json'
        plan.write_text(json.dumps({'session': 'fake', 'message': 'hello', 'targets': [], 'custom': 'keep'}))
        services.save_send_settings(True, 2, path=plan)
        value = services.read_json_object(plan)
        self.assertTrue(value['allow_repeat_same_day'])
        self.assertEqual(value['max_sends_per_conversation_per_day'], 2)
        self.assertEqual(value['custom'], 'keep')
        self.assertEqual(value['message'], 'hello')


class SendV2Integration(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_replay_conflict_and_quota_use_fake_websocket(self):
        import oneshot
        from test_streak import MockWebSocket, _build_synthetic_echo_frame
        target = {'conv_id': '0:1:111:222', 'conv_short_id': 123, 'conv_type': 1}
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'fixture.db')
            initialize_fixture(path)
            for index in (1, 2):
                client_id = f'client-{index}'
                echo = _build_synthetic_echo_frame(conv_id=target['conv_id'], sender_id='111',
                                                  client_msg_id=client_id, text='hello')
                ws = MockWebSocket(incoming_frames=[echo])
                with patch('oneshot.uuid.uuid4', return_value=client_id):
                    result = await oneshot._send_target_oneshot(ws, '111', target, 'hello', db_path=path,
                        idempotency_key=f'key-{index}', allow_repeat_same_day=True,
                        max_sends_per_conversation_per_day=2, min_send_interval_seconds=0)
                self.assertEqual(result['status'], 'confirmed')
                self.assertEqual(len(ws.sent_packets), 1)
            ws = MockWebSocket()
            result = await oneshot._send_target_oneshot(ws, '111', target, 'hello', db_path=path,
                idempotency_key='key-2', allow_repeat_same_day=True, max_sends_per_conversation_per_day=2, min_send_interval_seconds=0)
            self.assertEqual(result['reason'], 'already_confirmed')
            with self.assertRaises(requests.IdempotencyConflict):
                await oneshot._send_target_oneshot(ws, '111', target, 'different', db_path=path,
                                                  idempotency_key='key-2')
            result = await oneshot._send_target_oneshot(ws, '111', target, 'hello', db_path=path,
                idempotency_key='key-3', allow_repeat_same_day=True, max_sends_per_conversation_per_day=2, min_send_interval_seconds=0)
            self.assertIn('2/2', result['detail'])
            self.assertEqual(ws.sent_packets, [])

    async def test_packet_build_error_has_zero_transmission_quota(self):
        import oneshot
        from test_streak import MockWebSocket
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'fixture.db')
            initialize_fixture(path)
            ws = MockWebSocket()
            with patch.object(oneshot, 'build_ws_packet', side_effect=ValueError('synthetic')):
                result = await oneshot._send_target_oneshot(ws, '111',
                    {'conv_id': 'c', 'conv_short_id': 1, 'conv_type': 1}, 'hello', db_path=path)
            self.assertEqual(result['status'], 'failed_pretransmit')
            self.assertEqual(ws.sent_packets, [])
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT sum(transmitted) FROM send_requests').fetchone()[0], 0)
