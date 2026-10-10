"""Phase 3B: fixture-only rotation, administration and migration regression tests."""
import asyncio
from contextlib import closing, redirect_stdout
from copy import deepcopy
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import ledger
import ledger_migrations as migrations
import ledger_requests as requests
import rotation
import services
import main
import oneshot

TARGET = {'conv_id': 'c', 'conv_short_id': 1, 'conv_type': 1}
CONFIG = {'campaign_id': 'daily', 'rotation': {'mode': 'round_robin', 'templates': [
    {'id': 'a', 'text': '  Xin chào 🌞\n'}, {'id': 'b', 'text': 'Ngày mới!'}]}}


def race(path, action, barrier, output, data):
    try:
        barrier.wait(timeout=10)
        if action == 'enqueue':
            requests.create_request('u', 'c', data['key'], {'target': TARGET, 'message': ''}, db_path=path, rotation_config=CONFIG)
        elif action == 'cancel':
            requests.cancel_request(data['id'], 'race', db_path=path)
        elif action == 'claim':
            requests.start_transmission(data['id'], db_path=path, rotation_config=CONFIG)
        elif action == 'skip':
            requests.skip_selection(('u', 'c', 'daily'), data['preview'], 'race', db_path=path)
        elif action == 'success':
            row = requests.get_request(data['id'], db_path=path)
            requests.finish('u', 'c', row['date'], row['client_msg_id'], db_path=path,
                            status='confirmed', reason='echo', server_id='123')
        output.put('ok')
    except Exception as exc:
        output.put(type(exc).__name__)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'fixture.db')
        migrations.initialize(self.path)
        self.n = 0

    def create(self, cfg=None, uid='u', cid='c', key=None):
        self.n += 1
        return requests.create_request(uid, cid, key or str(self.n), {'target': dict(TARGET, conv_id=cid), 'message': ''},
            db_path=self.path, rotation_config=cfg or CONFIG)[0]

    def state(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute('SELECT success_seq,selection_generation FROM rotation_state').fetchall()

    def start(self, row, cfg=None):
        return requests.start_transmission(row['request_id'], db_path=self.path, rotation_config=cfg or CONFIG,
            allow_repeat_same_day=True, max_sends_per_conversation_per_day=20, min_send_interval_seconds=0,
            max_sends_per_window=20)

    def finish(self, row, status='confirmed'):
        row = requests.get_request(row['request_id'], db_path=self.path)
        requests.finish(row['canonical_uid'], row['conv_id'], row['date'], row['client_msg_id'], db_path=self.path,
                        status=status, reason='test', server_id='123' if status == 'confirmed' else None)

    def text(self, row):
        return json.loads(row['payload_json'])['message']

    def test_round_robin_advances_only_success_and_preserves_exact_text(self):
        a = self.create()
        self.assertEqual(self.text(a), '  Xin chào 🌞\n')
        self.assertEqual(self.state(), [(0, 0)])
        self.start(a)
        self.assertEqual(self.state(), [(0, 0)])
        self.finish(a)
        self.finish(a)  # Duplicate success is idempotent.
        self.assertEqual(self.state(), [(1, 0)])
        b = self.create()
        self.assertEqual(self.text(b), 'Ngày mới!')
        self.start(b)
        self.finish(b)
        self.assertEqual(self.text(self.create()), self.text(a))

    def test_idempotency_replay_before_scope_check_and_changed_input_conflicts(self):
        a = self.create(key='same')
        self.assertEqual(self.create(key='same')['request_id'], a['request_id'])
        changed = deepcopy(CONFIG)
        changed['rotation']['templates'][0]['text'] = 'Changed'
        with self.assertRaises(requests.IdempotencyConflict): self.create(changed, key='same')
        with self.assertRaises(ledger.LedgerError): self.create()

    def test_multiple_accounts_conversations_and_campaigns_are_independent(self):
        other = dict(CONFIG, campaign_id='other')
        rows = [self.create(), self.create(other), self.create(cid='d'), self.create(uid='v')]
        self.assertTrue(all(self.text(r) == self.text(rows[0]) for r in rows))
        self.assertEqual(len(self.state()), 4)

    def test_random_sticky_across_failed_cancelled_new_keys_and_restart(self):
        cfg = deepcopy(CONFIG)
        cfg['rotation']['mode'] = 'random_no_immediate_repeat'
        with patch.object(rotation.secrets, 'choice', return_value=rotation.catalog(cfg)['templates'][1]) as choose:
            a = self.create(cfg)
            requests.fail_pretransmit(a['request_id'], db_path=self.path)
            b = self.create(cfg)
            requests.cancel_request(b['request_id'], 'cancel', db_path=self.path)
            c = self.create(cfg)
            self.assertEqual(choose.call_count, 1)
        self.assertEqual(a['snapshot_json'], c['snapshot_json'])
        self.assertEqual(self.create(cfg, key=b['idempotency_key'])['status'], 'cancelled')
        self.start(c, cfg)
        self.finish(c)
        self.assertNotEqual(self.text(c), self.text(self.create(cfg)))

    def test_single_template_and_invalid_catalogs(self):
        cfg = deepcopy(CONFIG)
        cfg['rotation']['templates'] = cfg['rotation']['templates'][:1]
        for mode in ('round_robin', 'random_no_immediate_repeat'):
            cfg['rotation']['mode'] = mode
            cat = rotation.catalog(cfg)
            self.assertEqual(rotation.choose(cat, cat['templates'][0]), cat['templates'][0])
        for invalid in ({'message': 'x', **CONFIG}, {'rotation': {}}, {'message': '  '}, {'message': '\0'}):
            with self.assertRaises(ValueError): rotation.catalog(invalid)
        duplicate = deepcopy(CONFIG)
        duplicate['rotation']['templates'][1]['text'] = duplicate['rotation']['templates'][0]['text']
        with self.assertRaises(ValueError): rotation.catalog(duplicate)

    def test_cancel_is_terminal_and_cannot_cancel_pending_unknown_success(self):
        row = self.create()
        with self.assertRaises(ValueError): requests.cancel_request(row['request_id'], '', db_path=self.path)
        self.start(row)
        with self.assertRaises(ledger.LedgerError): requests.cancel_request(row['request_id'], 'no', db_path=self.path)
        self.finish(row, 'failed_unknown')
        with self.assertRaises(ledger.LedgerError): requests.cancel_request(row['request_id'], 'no', db_path=self.path)
        self.assertEqual(self.state(), [(0, 0)])
        next_row = self.create()
        with self.assertRaises(requests.RateLimitError): self.start(next_row)
        requests.cancel_request(next_row['request_id'], 'cancel', db_path=self.path)
        with self.assertRaises(ledger.LedgerError): requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)

    def test_snapshot_changes_hold_and_approval_is_bound_to_request_and_fingerprints(self):
        row = self.create()
        changed = deepcopy(CONFIG)
        changed['rotation']['templates'].pop(0)
        with self.assertRaisesRegex(ledger.LedgerError, 'TEMPLATE_CONFIG_CHANGED'): self.start(row, changed)
        review = requests.snapshot_review(row, changed)
        with self.assertRaises(ledger.LedgerError):
            requests.approve_snapshot(row['request_id'], changed, dict(review, request_id='wrong'), 'review', db_path=self.path)
        requests.approve_snapshot(row['request_id'], changed, review, 'accept exact snapshot', db_path=self.path)
        changed_again = deepcopy(changed)
        changed_again['rotation']['templates'][0]['text'] += '!'
        with self.assertRaisesRegex(ledger.LedgerError, 'TEMPLATE_CONFIG_CHANGED'): self.start(row, changed_again)
        self.assertTrue(self.start(row, changed))
        self.assertEqual(self.text(requests.get_request(row['request_id'], db_path=self.path)), self.text(row))

    def test_failed_choice_deleted_config_stays_held_until_skip(self):
        row = self.create()
        requests.fail_pretransmit(row['request_id'], db_path=self.path)
        changed = deepcopy(CONFIG)
        changed['rotation']['templates'].pop(0)
        next_row = self.create(changed)
        self.assertEqual(row['snapshot_json'], next_row['snapshot_json'])
        with self.assertRaises(ledger.LedgerError): self.start(next_row, changed)
        requests.cancel_request(next_row['request_id'], 'replace removed template', db_path=self.path)
        preview = requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        requests.skip_selection(('u', 'c', 'daily'), preview, 'template retired', db_path=self.path)
        self.assertEqual(self.state(), [(0, 1)])
        with self.assertRaises(ledger.LedgerError): requests.skip_selection(('u', 'c', 'daily'), preview, 'again', db_path=self.path)
        self.assertEqual(self.text(self.create(changed)), 'Ngày mới!')

    def test_skip_stale_preview_active_scope_and_audit_rollback(self):
        row = self.create()
        with self.assertRaises(ledger.LedgerError): requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        requests.cancel_request(row['request_id'], 'test', db_path=self.path)
        preview = requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        with patch.object(requests, 'rotation_event', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): requests.skip_selection(('u', 'c', 'daily'), preview, 'test', db_path=self.path)
        self.assertEqual(self.state(), [(0, 0)])
        requests.skip_selection(('u', 'c', 'daily'), preview, 'test', db_path=self.path)
        next_row = self.create()
        requests.cancel_request(next_row['request_id'], 'test', db_path=self.path)
        with self.assertRaisesRegex(ledger.LedgerError, 'Stale'):
            requests.skip_selection(('u', 'c', 'daily'), preview, 'test', db_path=self.path)

    def test_success_transaction_failure_recovers_unknown_without_rotation_advance(self):
        row = self.create()
        self.start(row)
        with patch.object(requests, 'rotation_event', side_effect=RuntimeError('crash after echo')):
            with self.assertRaises(RuntimeError): self.finish(row)
        self.assertEqual(self.state(), [(0, 0)])
        with ledger.RunLock(str(Path(self.tmp.name) / 'run.lock')) as lock:
            requests.recover_pending(db_path=self.path, run_lock=lock)
        self.assertEqual(requests.get_request(row['request_id'], db_path=self.path)['status'], 'failed_unknown')
        self.assertEqual(self.state(), [(0, 0)])

    def test_cancel_event_failure_rolls_back(self):
        row = self.create()
        with patch.object(requests, 'event', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): requests.cancel_request(row['request_id'], 'test', db_path=self.path)
        self.assertEqual(requests.get_request(row['request_id'], db_path=self.path)['status'], 'queued')

    def test_snapshot_approval_does_not_bypass_quota(self):
        row = self.create()
        self.start(row)
        self.finish(row)
        another = self.create()
        with self.assertRaises(requests.RateLimitError):
            requests.start_transmission(another['request_id'], db_path=self.path, rotation_config=CONFIG)
        self.assertEqual(self.state(), [(1, 0)])

    def compete(self, actions):
        ctx = multiprocessing.get_context('spawn')
        barrier, output = ctx.Barrier(2), ctx.Queue()
        children = [ctx.Process(target=race, args=(self.path, action, barrier, output, data)) for action, data in actions]
        try:
            for child in children: child.start()
            results = [output.get(timeout=20) for _ in children]
            for child in children:
                child.join(20)
                self.assertEqual(child.exitcode, 0)
            return results
        finally:
            for child in children:
                if child.is_alive(): child.terminate()
                child.join()
            output.close()

    def test_two_process_enqueue_only_one_active_request(self):
        results = self.compete([('enqueue', {'key': 'a'}), ('enqueue', {'key': 'b'})])
        self.assertEqual(results.count('ok'), 1)

    def test_cancel_vs_claim_never_cancels_transmitted_request(self):
        row = self.create()
        self.compete([('cancel', {'id': row['request_id']}), ('claim', {'id': row['request_id']})])
        stored = requests.get_request(row['request_id'], db_path=self.path)
        self.assertIn((stored['status'], stored['transmitted']), [('cancelled', 0), ('pending', 1)])

    def test_skip_vs_enqueue_preserves_consistent_generation(self):
        row = self.create()
        requests.cancel_request(row['request_id'], 'test', db_path=self.path)
        preview = requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        self.compete([('skip', {'preview': preview}), ('enqueue', {'key': 'next'})])
        with closing(sqlite3.connect(self.path)) as conn:
            active = conn.execute("SELECT selection_generation FROM send_requests WHERE status='queued'").fetchone()
        self.assertEqual(active[0], self.state()[0][1])

    def test_dryrun_no_writes_and_cli_skip_requires_tty(self):
        cfg = dict(CONFIG, session='fixture', targets=[TARGET])
        plan = Path(self.tmp.name) / 'plan.json'
        plan.write_text(json.dumps(cfg), encoding='utf-8')
        with closing(sqlite3.connect(self.path)) as conn:
            before = list(conn.iterdump())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_dry_run(str(plan)), 0)
                with patch('sys.argv', ['main.py', 'rotation-skip-selection', '--db', self.path, '--account', 'u', '--conversation', 'c', '--campaign', 'daily']), patch('sys.stdin.isatty', return_value=False):
                    self.assertEqual(main.main(), 1)
            self.assertEqual(list(conn.iterdump()), before)

    def test_cancel_vs_success_preserves_success_and_advances_once(self):
        row = self.create()
        self.start(row)
        results = self.compete([('cancel', {'id': row['request_id']}), ('success', {'id': row['request_id']})])
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual(self.state(), [(1, 0)])
        self.assertEqual(requests.get_request(row['request_id'], db_path=self.path)['status'], 'confirmed')

    def test_skip_vs_success_rejects_stale_unfinished_step(self):
        row = self.create()
        requests.cancel_request(row['request_id'], 'preview', db_path=self.path)
        preview = requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        replacement = self.create()
        self.start(replacement)
        results = self.compete([('skip', {'preview': preview}), ('success', {'id': replacement['request_id']})])
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual(self.state(), [(1, 0)])

    def test_enqueue_vs_success_uses_committed_success_version(self):
        row = self.create()
        self.start(row)
        self.compete([('enqueue', {'key': 'next'}), ('success', {'id': row['request_id']})])
        with closing(sqlite3.connect(self.path)) as conn:
            active = conn.execute("SELECT rotation_seq,payload_json FROM send_requests WHERE status='queued'").fetchone()
        if active:
            self.assertEqual(active[0], 1)
            self.assertEqual(json.loads(active[1])['message'], 'Ngày mới!')
        self.assertEqual(self.state(), [(1, 0)])

    def test_skip_after_last_success_and_new_catalog_uses_last_success(self):
        row = self.create()
        self.start(row)
        self.finish(row)
        failed = self.create()
        requests.fail_pretransmit(failed['request_id'], db_path=self.path)
        preview = requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        requests.skip_selection(('u', 'c', 'daily'), preview, 'retired', db_path=self.path)
        cfg = deepcopy(CONFIG)
        cfg['rotation']['templates'].insert(1, {'id': 'new', 'text': 'New middle'})
        self.assertEqual(self.text(self.create(cfg)), 'New middle')
        self.assertEqual(self.state(), [(1, 1)])

    def test_random_avoids_last_success_id_and_exact_content(self):
        cat = rotation.catalog(CONFIG)
        cat['mode'] = 'random_no_immediate_repeat'
        last = cat['templates'][0]
        for _ in range(10):
            self.assertEqual(rotation.choose(cat, last), cat['templates'][1])
        cat['templates'][0]['id'] = 'renamed'
        self.assertEqual(rotation.choose(cat, last)['id'], 'b')

    def test_claim_requires_current_catalog_even_after_restart(self):
        row = self.create()
        with self.assertRaises(ledger.LedgerError):
            requests.start_transmission(row['request_id'], db_path=self.path)
        stored = requests.get_request(row['request_id'], db_path=self.path)
        self.assertEqual((stored['status'], stored['transmitted']), ('queued', 0))

    def test_snapshot_and_history_are_database_immutable(self):
        row = self.create()
        requests.cancel_request(row['request_id'], 'test', db_path=self.path)
        preview = requests.preview_skip(('u', 'c', 'daily'), db_path=self.path)
        requests.skip_selection(('u', 'c', 'daily'), preview, 'test', db_path=self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            for sql in ("UPDATE send_requests SET snapshot_json='{}'", 'DELETE FROM send_requests',
                        'DELETE FROM rotation_events', "UPDATE rotation_events SET details_json='{}'"):
                with self.assertRaises(sqlite3.IntegrityError): conn.execute(sql)

    def test_approval_preview_invalidated_by_current_configuration_change(self):
        row = self.create()
        changed = deepcopy(CONFIG)
        changed['rotation']['templates'].reverse()
        preview = requests.snapshot_review(row, changed)
        changed['rotation']['templates'][0]['text'] += 'new'
        with self.assertRaisesRegex(ledger.LedgerError, 'Stale'):
            requests.approve_snapshot(row['request_id'], changed, preview, 'test', db_path=self.path)

    def test_cli_yes_does_not_authorize_skip_or_snapshot_approval(self):
        import sys
        with patch('sys.stdin.isatty', return_value=False), redirect_stdout(io.StringIO()):
            args = ['main.py', '--yes', 'rotation-skip-selection', '--db', self.path,
                    '--account', 'u', '--conversation', 'c', '--campaign', 'daily']
            with patch.object(sys, 'argv', args):
                self.assertEqual(main.main(), 1)

    def test_cli_interactive_skip_and_snapshot_approval_record_history_without_send(self):
        import sys
        row = self.create()
        plan = Path(self.tmp.name) / 'plan.json'
        cfg = deepcopy(CONFIG)
        cfg['rotation']['templates'].reverse()
        plan.write_text(json.dumps(cfg), encoding='utf-8')
        args = ['main.py', 'request-approve-snapshot', '--db', self.path, '--request-id', row['request_id'], '--config', str(plan)]
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), redirect_stdout(io.StringIO()):
            with patch.object(sys, 'argv', args), patch('builtins.input', side_effect=['reviewed', 'request-approve-snapshot']):
                # redirect_stdout has its own isatty; explicitly patch the replacement stream.
                with patch.object(sys.stdout, 'isatty', return_value=True):
                    self.assertEqual(main.main(), 0)
            requests.cancel_request(row['request_id'], 'retired', db_path=self.path)
            args = ['main.py', 'rotation-skip-selection', '--db', self.path, '--account', 'u', '--conversation', 'c', '--campaign', 'daily']
            with patch.object(sys, 'argv', args), patch('builtins.input', side_effect=['retired', 'rotation-skip-selection']), patch.object(sys.stdout, 'isatty', return_value=True):
                self.assertEqual(main.main(), 0)
        self.assertEqual(self.state(), [(0, 1)])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT sum(transmitted) FROM send_requests').fetchone()[0], 0)


class RotationMigrationTests(unittest.TestCase):
    def test_v3_wal_backup_restore_history_and_repeated_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'v3.db')
            with closing(sqlite3.connect(path, isolation_level=None)) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                migrations._migrate_v2(conn, path)
                conn.execute('BEGIN IMMEDIATE')
                migrations._apply_v3(conn, None)
                conn.commit()
                conn.execute("INSERT INTO send_requests(request_id,canonical_uid,conv_id,idempotency_key,payload_json,status,created_at,client_msg_id) VALUES('r','u','c','k','{}','queued','now','client')")
                conn.execute("INSERT INTO request_events(request_id,event_type,created_at,details_json) VALUES('r','CREATED','now','{}')")
                before = conn.execute('SELECT * FROM send_requests').fetchall()
                events = conn.execute('SELECT * FROM request_events').fetchall()
                dump = list(conn.iterdump())
                original = migrations._apply_v4
                def fail(*args):
                    original(*args)
                    raise RuntimeError('crash before commit')
                with patch.object(migrations, '_apply_v4', side_effect=fail):
                    with self.assertRaises(RuntimeError): migrations.migrate(path)
                self.assertEqual(list(conn.iterdump()), dump)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
                backup = migrations.migrate(path)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 5)
                self.assertEqual([r[:16] for r in conn.execute('SELECT * FROM send_requests')], before)
                self.assertEqual(conn.execute('SELECT * FROM request_events').fetchall(), events)
                self.assertIsNone(conn.execute('PRAGMA foreign_key_check').fetchone())
                self.assertIsNone(migrations.migrate(path))
                with closing(sqlite3.connect(backup)) as saved, closing(sqlite3.connect(str(Path(tmp) / 'restored.db'))) as restored:
                    saved.backup(restored)
                    self.assertEqual(restored.execute('PRAGMA user_version').fetchone()[0], 3)
                    self.assertEqual(restored.execute('SELECT * FROM send_requests').fetchall(), before)


class RotationSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_packet_and_echo_use_exact_snapshot_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'fixture.db')
            migrations.initialize(path)
            row, _ = requests.create_request('u', 'c', 'key', {'target': TARGET, 'message': ''}, db_path=path, rotation_config=CONFIG)
            changed = deepcopy(CONFIG)
            changed['rotation']['templates'][0]['text'] = 'new'
            requests.approve_snapshot(row['request_id'], changed, requests.snapshot_review(row, changed), 'accept old', db_path=path)
            ws = AsyncMock()
            ws.recv.return_value = b'frame'
            exact = json.loads(row['payload_json'])['message']
            with patch.object(oneshot, 'build_ws_packet', return_value=(b'packet', 1, None)) as packet, \
                 patch.object(oneshot.LttkClient, '_decompress_lz4_frame', return_value=None), \
                 patch.object(oneshot.LttkClient, 'find_all_msgbodies', return_value=[{}]), \
                 patch.object(oneshot.LttkClient, 'check_echo_correlation', return_value=(True, 'ok', '123')) as echo:
                result = await oneshot._send_target_oneshot(ws, 'u', TARGET, 'WRONG', db_path=path,
                    existing_request=row['request_id'], rotation_config=changed)
            self.assertEqual(result['status'], 'confirmed')
            self.assertEqual(packet.call_args.kwargs['text'], exact)
            self.assertEqual(echo.call_args.kwargs['expected_text'], exact)

    async def test_changed_snapshot_without_approval_never_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'fixture.db')
            migrations.initialize(path)
            row, _ = requests.create_request('u', 'c', 'key', {'target': TARGET, 'message': ''}, db_path=path, rotation_config=CONFIG)
            changed = deepcopy(CONFIG)
            changed['rotation']['templates'].reverse()
            ws = AsyncMock()
            result = await oneshot._send_target_oneshot(ws, 'u', TARGET, '', db_path=path,
                existing_request=row['request_id'], rotation_config=changed)
            self.assertEqual(result['reason'], 'snapshot_held')
            ws.send.assert_not_called()
