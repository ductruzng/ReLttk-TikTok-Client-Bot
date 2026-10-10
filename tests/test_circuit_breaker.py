"""Offline circuit tests. All databases are disposable and transports synthetic."""
import asyncio
import contextlib
import io
import json
import multiprocessing
import sqlite3
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from urllib.error import HTTPError, URLError

import circuit_breaker as cb
import ledger
import ledger_migrations as migrations
import ledger_requests as requests
import main
from tests.test_streak import MockWebSocket, _build_synthetic_echo_frame


def compete(path, rid, action, ready, go, output):
    ready.set()
    go.wait(10)
    try:
        if action == 'trip':
            cb.record_failure(cb.Failure('NETWORK', 'handshake', 'transport'), db_path=path, uid='111')
        else:
            requests.start_transmission(rid, db_path=path, min_send_interval_seconds=0)
        output.put(action)
    except cb.CircuitOpen:
        output.put('blocked')


class CircuitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'ledger.db')
        migrations.initialize(self.path)

    def query(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(sql, params).fetchall()

    def request(self, key='key', conv='c', uid='111'):
        return requests.create_request(uid, conv, key, {'message': 'hello'}, db_path=self.path)[0]

    def start(self, row):
        return requests.start_transmission(row['request_id'], db_path=self.path, min_send_interval_seconds=0)

    def finish(self, row, status='failed_unknown'):
        stored = requests.get_request(row['request_id'], db_path=self.path)
        requests.finish(stored['canonical_uid'], stored['conv_id'], stored['date'], stored['client_msg_id'],
                        db_path=self.path, status=status, reason='UNKNOWN', server_id='123' if status == 'confirmed' else None)

    def trip(self, uid='111'):
        cb.record_failure(cb.Failure('NETWORK', 'handshake', 'transport'), db_path=self.path, uid=uid)

    def test_typed_http_classification_and_redaction(self):
        for code, category in [(401,'AUTH'),(403,'ACCESS_DENIED_UNCLASSIFIED'),(429,'SERVER_RATE_LIMIT'),(503,'SERVER_UNAVAILABLE'),(499,'SERVER_ERROR_UNCLASSIFIED')]:
            with self.subTest(code=code):
                failure = cb.classify(HTTPError('https://invalid/?token=SECRET',code,'SECRET',{},None),'identity')
                self.assertEqual(failure.category, category)
                self.assertNotIn('SECRET',json.dumps(failure.evidence()))
        self.assertIsNone(cb.classify(ValueError('ban error restricted'), 'local'))
        self.assertEqual(cb.classify(URLError(ssl.SSLError('secret')), 'identity').category, 'TLS')
        self.assertEqual(cb.classify(TimeoutError(), 'echo').category, 'NETWORK')

    def test_retry_after_is_hint_only(self):
        with patch.object(cb,'now_ms',return_value=1000):
            for value, expected in [('60',61000),('-1',None),('garbage',None)]:
                f = cb.classify(HTTPError('https://invalid',429,'',{'Retry-After':value},None),'handshake')
                self.assertEqual(f.retry_not_before_ms,expected)
        self.trip()
        with patch.object(cb,'now_ms',return_value=9999999999999):
            with self.assertRaises(cb.CircuitOpen): cb.gate(db_path=self.path,uid='111')

    def test_open_claim_does_not_consume_quota_or_change_queue(self):
        row=self.request(); self.trip()
        with self.assertRaises(cb.CircuitOpen): self.start(row)
        stored=requests.get_request(row['request_id'],db_path=self.path)
        self.assertEqual((stored['status'],stored['transmitted']),('queued',0))

    def test_unknown_atomic_and_reset_denied(self):
        row=self.request(); self.start(row); self.finish(row)
        state=cb.inspect(db_path=self.path,uid='111')[0]
        self.assertEqual(state['state'],'OPEN')
        self.assertEqual(self.query('SELECT sum(transmitted) FROM send_requests'),[(1,)])
        with self.assertRaisesRegex(ledger.LedgerError,'UNKNOWN'):
            cb.reset('account','111',state,'reviewed',db_path=self.path)

    def test_unknown_transaction_rolls_back_if_breaker_write_fails(self):
        row=self.request(); self.start(row)
        with patch.object(cb,'trip',side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError): self.finish(row)
        self.assertEqual(requests.get_request(row['request_id'],db_path=self.path)['status'],'pending')
        self.assertEqual(cb.inspect(db_path=self.path,uid='111'),[])
        with ledger.RunLock(str(Path(self.tmp.name)/'run.lock')) as lock:
            self.assertEqual(requests.recover_pending(db_path=self.path,run_lock=lock),1)
            self.assertEqual(requests.recover_pending(db_path=self.path,run_lock=lock),0)
        self.assertEqual(len(cb.inspect(db_path=self.path,uid='111',history=True)),1)

    def test_reset_stale_version_and_audit_immutable(self):
        self.trip(); old=cb.inspect(db_path=self.path,uid='111')[0]; self.trip()
        with self.assertRaisesRegex(ledger.LedgerError,'Stale'):
            cb.reset('account','111',old,'reviewed',db_path=self.path)
        current=cb.inspect(db_path=self.path,uid='111')[0]
        cb.reset('account','111',current,'reviewed',db_path=self.path)
        cb.gate(db_path=self.path,uid='111')
        self.assertEqual(len(cb.inspect(db_path=self.path,uid='111',history=True)),3)
        with self.assertRaises(sqlite3.IntegrityError): self.query('DELETE FROM breaker_events')

    def test_pending_blocks_reset_and_late_success_does_not_close(self):
        row=self.request(); self.start(row); self.trip()
        with self.assertRaisesRegex(ledger.LedgerError,'Pending'):
            cb.reset('account','111',cb.inspect(db_path=self.path,uid='111')[0],'review',db_path=self.path)
        self.finish(row,'confirmed')
        with self.assertRaises(cb.CircuitOpen): cb.gate(db_path=self.path,uid='111')

    def test_bound_session_gate_and_scope_isolation(self):
        cb.bind_session('111','s',db_path=self.path)
        self.trip()
        with self.assertRaises(cb.CircuitOpen): cb.gate(db_path=self.path,session='s')
        cb.gate(db_path=self.path,uid='222',session='other')
        cb.record_failure(cb.Failure('AUTH','identity','http',401),db_path=self.path,session='s')
        preview=cb.inspect(db_path=self.path,session='s')[0]
        cb.reset('session','s',preview,'review',db_path=self.path)
        with self.assertRaises(cb.CircuitOpen): cb.gate(db_path=self.path,session='s')

    def test_clock_rollback_blocks_reset(self):
        self.trip(); preview=cb.inspect(db_path=self.path,uid='111')[0]
        with patch.object(cb,'now_ms',return_value=0):
            with self.assertRaises(ledger.LedgerError): cb.reset('account','111',preview,'review',db_path=self.path)

    def test_replay_and_pretransmit_failure_unchanged(self):
        row=self.request(); requests.fail_pretransmit(row['request_id'],db_path=self.path)
        replay,created=requests.create_request('111','c','key',{'message':'hello'},db_path=self.path)
        self.assertFalse(created); self.assertEqual(row['request_id'],replay['request_id'])
        self.assertEqual(cb.inspect(db_path=self.path,uid='111'),[])
        self.assertEqual(self.query('SELECT sum(transmitted) FROM send_requests'),[(0,)])

    def test_cli_readonly_missing_and_noninteractive_reset(self):
        self.trip()
        for command in ('breaker-status','breaker-history'):
            with patch('sys.argv',['main.py',command,'--db',self.path,'--account','111']),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(),0)
        with patch('sys.argv',['main.py','breaker-reset','--db',self.path,'--account','111']),patch('sys.stdin.isatty',return_value=False),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(),1)
        missing=str(Path(self.tmp.name)/'missing.db')
        with self.assertRaises(ledger.LedgerError): cb.inspect(db_path=missing,uid='111')
        self.assertFalse(Path(missing).exists())

    def test_two_process_trip_claim_serialization(self):
        row=self.request(); ctx=multiprocessing.get_context('spawn')
        go=ctx.Event(); out=ctx.Queue(); ready=[ctx.Event(),ctx.Event()]
        procs=[ctx.Process(target=compete,args=(self.path,row['request_id'],action,ready[i],go,out)) for i,action in enumerate(('trip','claim'))]
        for p in procs:p.start()
        for e in ready:self.assertTrue(e.wait(10))
        go.set()
        for p in procs:p.join(15); self.assertEqual(p.exitcode,0)
        results={out.get(timeout=2),out.get(timeout=2)}
        self.assertIn('trip',results)
        stored=requests.get_request(row['request_id'],db_path=self.path)
        self.assertEqual(stored['transmitted'],int('claim' in results))
        with self.assertRaises(cb.CircuitOpen): cb.gate(db_path=self.path,uid='111')


class MigrationTests(unittest.TestCase):
    def test_v4_wal_unknown_pending_backup_rollback_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'v4.db')
            with contextlib.closing(sqlite3.connect(path,isolation_level=None)) as conn:
                conn.execute('PRAGMA journal_mode=WAL'); conn.execute('PRAGMA wal_autocheckpoint=0')
                migrations._migrate_v2(conn,path)
                conn.execute('BEGIN IMMEDIATE'); migrations._apply_v3(conn,None); migrations._apply_v4(conn,None); conn.commit()
                for i,status in enumerate(('failed_unknown','pending')):
                    conn.execute("""INSERT INTO send_requests(request_id,canonical_uid,conv_id,idempotency_key,payload_json,status,created_at,date,attempt_time,client_msg_id,transmitted,transmission_started_at_ms)
                        VALUES(?,?,?,?,?,?,?,'2026-10-09','2026-10-09T12:00:00+07:00',?,1,1791522000000)""",(str(i),'111',str(i),str(i),'{}',status,'fixture',str(i)))
                before=list(conn.iterdump()); rows=conn.execute('SELECT * FROM send_requests').fetchall()
                report=migrations.preflight(path)
                self.assertEqual((report['unknown_count'],report['pending_count']),(1,1))
                original=migrations._apply_v5
                def fail(*args): original(*args); raise RuntimeError('rollback fixture')
                with patch.object(migrations,'_apply_v5',side_effect=fail):
                    with self.assertRaises(RuntimeError): migrations.migrate(path)
                self.assertEqual(list(conn.iterdump()),before)
                backup=migrations.migrate(path)
                self.assertEqual(conn.execute('SELECT * FROM send_requests').fetchall(),rows)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertIsNone(migrations.migrate(path))
                events=cb.inspect(db_path=path,uid='111',history=True)
                self.assertEqual(events[0]['source_request_id'],'0')
                self.assertEqual(events[0]['event_type'],'LEGACY_UNKNOWN_IMPORTED')
                with contextlib.closing(sqlite3.connect(backup)) as saved,contextlib.closing(sqlite3.connect(str(Path(tmp)/'restore.db'))) as dest:
                    saved.backup(dest)
                    self.assertEqual(dest.execute('PRAGMA user_version').fetchone()[0],4)
                    self.assertEqual(dest.execute('SELECT * FROM send_requests').fetchall(),rows)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_keywords_are_not_restrictions(self):
        from oneshot import _send_target_oneshot
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db'); migrations.initialize(path)
            target={'conv_id':'0:1:111:222','conv_short_id':7,'conv_type':1}
            text='ban error restricted "status_code": 123'
            ws=MockWebSocket([_build_synthetic_echo_frame(target['conv_id'],'111','client',text)])
            with patch('uuid.uuid4',return_value='client'):
                result=await _send_target_oneshot(ws,'111',target,text,db_path=path)
            self.assertEqual(result['status'],'confirmed')
            self.assertEqual(cb.inspect(db_path=path,uid='111'),[])

    async def test_send_failure_opens_and_preserves_marker(self):
        from oneshot import _send_target_oneshot
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db'); migrations.initialize(path)
            result=await _send_target_oneshot(MockWebSocket(send_exc=ConnectionResetError()),'111',
                {'conv_id':'c','conv_short_id':7,'conv_type':1},'hello',db_path=path)
            self.assertEqual(result['status'],'failed_unknown')
            self.assertEqual(cb.inspect(db_path=path,uid='111')[0]['category'],'NETWORK')

    async def test_open_session_stops_before_credentials_or_network(self):
        import oneshot
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db'); migrations.initialize(path)
            plan=Path(tmp)/'plan.json'; plan.write_text(json.dumps({'session':'s','message':'hi','targets':[{'conv_id':'c','conv_short_id':7,'conv_type':1}]}))
            cb.record_failure(cb.Failure('AUTH','identity','http',401),db_path=path,session='s')
            with patch.object(oneshot,'load_session') as load,patch.object(oneshot,'_open_ws',AsyncMock()) as connect:
                with self.assertRaises(cb.CircuitOpen): await oneshot.run_oneshot_send(str(plan),db_path=path)
                load.assert_not_called();connect.assert_not_called()

    async def test_batch_unknown_stops_without_enqueuing_remaining_rotation(self):
        import oneshot
        class Connection(MockWebSocket):
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db'); migrations.initialize(path)
            cfg={'session':'s','campaign_id':'daily','rotation':{'mode':'round_robin',
                 'templates':[{'id':'a','text':'hello'},{'id':'b','text':'world'}]},
                 'targets':[{'conv_id':c,'conv_short_id':7,'conv_type':1} for c in ('c','d')]}
            plan=Path(tmp)/'plan.json'; plan.write_text(json.dumps(cfg))
            ws=Connection()
            with patch.object(oneshot,'load_session',return_value={'sessionid':'fixture'}), \
                 patch.object(oneshot,'get_own_user_id',return_value='111'), \
                 patch.object(oneshot,'load_ws_auth',return_value={'device_id':'0'}), \
                 patch.object(oneshot,'_prepare_ws',return_value=({},'wss://fixture.invalid')), \
                 patch.object(oneshot,'_open_ws',AsyncMock(return_value=ws)):
                result=await oneshot.run_oneshot_send(str(plan),db_path=path,validate_server=False,timeout_seconds=.01)
            self.assertEqual([r['status'] for r in result['results']],['failed_unknown','not_attempted'])
            with contextlib.closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM send_requests').fetchone()[0],1)
                self.assertEqual(conn.execute('SELECT success_seq FROM rotation_state').fetchall(),[(0,)])

    async def test_pre_auth_http_failure_and_restart_no_network(self):
        import oneshot
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db'); migrations.initialize(path)
            plan=Path(tmp)/'plan.json';plan.write_text(json.dumps({'session':'s','message':'hi','targets':[{'conv_id':'c','conv_short_id':7,'conv_type':1}]}))
            with patch.object(oneshot,'load_session',return_value={'sessionid':'fixture'}), \
                 patch.object(oneshot,'get_own_user_id',side_effect=HTTPError('https://invalid',401,'SECRET',{},None)) as identity:
                for _ in range(2):
                    with self.assertRaises(cb.CircuitOpen): await oneshot.run_oneshot_send(str(plan),db_path=path)
                identity.assert_called_once()
            self.assertEqual(cb.inspect(db_path=path,session='s')[0]['category'],'AUTH')
            with contextlib.closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM send_requests').fetchone()[0],0)

    async def test_decoder_unknown_text_and_corrupt_bytes_never_confirm(self):
        from client import LttkClient
        self.assertEqual(LttkClient.decode_echo_frame(b'{"error_code":123,"ban":true}'),[])
        self.assertEqual(LttkClient.decode_echo_frame(b'\x04\x22\x4d\x18bad'), [])


    async def test_cancelled_transport_records_unknown_atomically(self):
        from oneshot import _send_target_oneshot
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db'); migrations.initialize(path)
            with self.assertRaises(asyncio.CancelledError):
                await _send_target_oneshot(MockWebSocket(send_exc=asyncio.CancelledError()),'111',
                    {'conv_id':'c','conv_short_id':7,'conv_type':1},'hello',db_path=path)
            self.assertEqual(cb.inspect(db_path=path,uid='111')[0]['state'],'OPEN')
            with contextlib.closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT status,transmitted FROM send_requests').fetchall(),[('failed_unknown',1)])


class IdentityCacheTests(unittest.TestCase):
    def test_fresh_identity_never_opens_cache_on_success_or_parse_failure(self):
        from core import api
        from unittest.mock import MagicMock
        for body,valid in [(b'{"odinId":"111"}',True),(b'no identity',False)]:
            with self.subTest(valid=valid):
                response=MagicMock()
                response.__enter__.return_value.read.return_value=body
                with patch.object(api.urllib.request,'urlopen',return_value=response) as transport, \
                     patch('builtins.open',side_effect=AssertionError('Cache I/O forbidden')) as opened:
                    if valid:
                        self.assertEqual(api.get_own_user_id({'sessionid':'fixture'},use_cache=False),'111')
                    else:
                        with self.assertRaises(cb.ProtocolFailure):
                            api.get_own_user_id({'sessionid':'fixture'},use_cache=False)
                    opened.assert_not_called()
                    transport.assert_called_once()

    def test_default_cache_behavior_still_works_with_fixture(self):
        from core import api
        from unittest.mock import mock_open
        import hashlib
        data=json.dumps({'session_hash':hashlib.sha256(b'fixture').hexdigest()[:16],'uid':'222'})
        with patch('builtins.open',mock_open(read_data=data)),patch.object(api.urllib.request,'urlopen') as transport:
            self.assertEqual(api.get_own_user_id({'sessionid':'fixture'}),'222')
            transport.assert_not_called()
