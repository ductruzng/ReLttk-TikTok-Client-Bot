"""CLI regressions; all plans, databases and transport are fixtures."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import cli
import main
import services
import ledger_migrations
import ledger_requests
import oneshot
from test_rotation import CONFIG, TARGET


class CLIHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name) / 'plan.json'

    def result(self, rows):
        output = io.StringIO()
        with patch.object(services, 'send_plan', AsyncMock(return_value={'results': rows})), redirect_stdout(output):
            code = main.cmd_send('unused-fixture.json')
        return code, output.getvalue()

    def test_rotation_menu_preserves_file_without_prompting_for_message(self):
        self.plan.write_text(json.dumps(CONFIG), encoding='utf-8')
        original = self.plan.read_bytes()
        with patch.object(cli, 'CONFIG_PATH', self.plan), patch('builtins.input', return_value='') as prompt, redirect_stdout(io.StringIO()) as out:
            cli.menu_config_message()
        self.assertEqual(prompt.call_count, 1)
        self.assertIn('rotation.templates', out.getvalue())
        self.assertEqual(self.plan.read_bytes(), original)

    def test_fixed_message_menu_still_saves(self):
        self.plan.write_text(json.dumps({'message': 'old', 'custom': 'keep'}), encoding='utf-8')
        with patch.object(cli, 'CONFIG_PATH', self.plan), patch('builtins.input', side_effect=['new', '']), redirect_stdout(io.StringIO()):
            cli.menu_config_message()
        self.assertEqual(json.loads(self.plan.read_text()), {'message': 'new', 'custom': 'keep'})

    def test_invalid_message_or_rotation_rejected_before_write(self):
        for cfg, text in [({'message': 'old'}, '\0'), (CONFIG, 'new'), ({'message': 'old', 'max_sends_per_window': 0}, 'new')]:
            self.plan.write_text(json.dumps(cfg), encoding='utf-8')
            original = self.plan.read_bytes()
            with self.assertRaises(ValueError): services.save_fixed_message(text, self.plan)
            self.assertEqual(self.plan.read_bytes(), original)

    def test_status_exit_codes_and_labels(self):
        for status in ('queued', 'pending', 'skipped', 'failed_pretransmit', 'failed_unknown', 'cancelled', None):
            with self.subTest(status=status):
                code, text = self.result([{'status': status, 'conv_id': 'c'}])
                self.assertEqual(code, 1)
                if status != 'failed_unknown': self.assertNotIn('FAILED_UNKNOWN', text)
        self.assertEqual(self.result([])[0], 1)
        self.assertEqual(self.result([{'status': 'confirmed'}])[0], 0)

    def test_only_confirmed_replay_counts_as_complete(self):
        for stored in ('confirmed', 'queued', 'pending', 'cancelled', 'failed_pretransmit', 'failed_unknown'):
            code, output = self.result([{'status': 'skipped', 'existing_status': stored}])
            self.assertEqual(code, 0 if stored == 'confirmed' else 1)
            self.assertIn('request=' + stored.upper(), output)

    def test_partial_batch_is_not_complete(self):
        success = {'status': 'confirmed', 'conv_id': 'a'}
        for reason in ('quota_claimed', 'snapshot_held', 'confirmation_declined'):
            self.assertEqual(self.result([success, {'status': 'skipped', 'existing_status': 'queued', 'reason': reason}])[0], 1)
        self.assertEqual(self.result([success, {'status': 'skipped', 'existing_status': 'confirmed'}])[0], 0)

    def test_real_fixture_holds_and_pretransmit_failure_report_correctly(self):
        for mode in ('snapshot', 'quota', 'pretransmit', 'declined'):
            path = str(Path(self.tmp.name) / (mode + '.db'))
            ledger_migrations.initialize(path)
            row, _ = ledger_requests.create_request('u', 'c', 'k', {'target': TARGET, 'message': ''}, db_path=path, rotation_config=CONFIG)
            cfg = deepcopy(CONFIG)
            if mode == 'snapshot': cfg['rotation']['templates'].reverse()
            if mode == 'quota':
                other, _ = ledger_requests.create_request('u', 'other', 'other', {}, db_path=path)
                ledger_requests.start_transmission(other['request_id'], db_path=path)
            ws = AsyncMock()
            options = dict(existing_request=row['request_id'], rotation_config=cfg)
            if mode == 'declined': options['confirm_request'] = lambda _: False
            with patch.object(oneshot, 'build_ws_packet', side_effect=ValueError('fixture') if mode == 'pretransmit' else None,
                              return_value=(b'fixture', 1, None)):
                result = asyncio.run(oneshot._send_target_oneshot(ws, 'u', TARGET, '', db_path=path, **options))
            ws.send.assert_not_called()
            code, output = self.result([result])
            self.assertEqual(code, 1)
            self.assertNotIn('FAILED_UNKNOWN', output)
            self.assertEqual(result.get('existing_status', result['status']), 'failed_pretransmit' if mode == 'pretransmit' else 'queued')

    def test_confirmed_replay_never_transmits_and_returns_success(self):
        path = str(Path(self.tmp.name) / 'fixture.db')
        ledger_migrations.initialize(path)
        row, _ = ledger_requests.create_request('u', 'c', 'k', {'target': TARGET, 'message': 'hello'}, db_path=path)
        ledger_requests.start_transmission(row['request_id'], db_path=path)
        current = ledger_requests.get_request(row['request_id'], db_path=path)
        ledger_requests.finish('u', 'c', current['date'], row['client_msg_id'], db_path=path,
                               status='confirmed', reason='echo', server_id='123')
        ws = AsyncMock()
        result = asyncio.run(oneshot._send_target_oneshot(ws, 'u', TARGET, 'hello', db_path=path, idempotency_key='k'))
        ws.send.assert_not_called()
        self.assertEqual(result['existing_status'], 'confirmed')
        self.assertEqual(result['server_msg_id'], '123')
        self.assertEqual(self.result([result])[0], 0)
