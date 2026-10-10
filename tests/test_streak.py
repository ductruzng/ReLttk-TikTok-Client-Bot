import asyncio
import concurrent.futures
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Ensure repo root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ledger
from ledger_fixtures import reserve_at, initialize_fixture
import log
from core.proto import _build_msg_body, build_ws_packet, f_varint, f_str, f_bytes, encode_varint
from client import LttkClient
from oneshot import validate_config_data, _send_target_oneshot


class TestCliDryRun(unittest.TestCase):
    """Test CLI default dry-run behavior with python -B (stdlib only)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_dryrun_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_cli_unconfigured_streak_json(self):
        """Default python -B main.py should run with stdlib only and exit cleanly with validation notes."""
        proc = subprocess.run(
            [sys.executable, "-B", "main.py"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Plan (OFFLINE DRY-RUN)", proc.stdout)
        self.assertIn("Field 'session' is empty", proc.stdout)
        self.assertIn("Field 'targets' is empty", proc.stdout)
        self.assertIn("Dry-run stopped due to configuration errors", proc.stdout)

        # Confirm no state directory or streak_ledger.db was created during dry run
        state_dir = os.path.join(_REPO_ROOT, "state")
        ledger_db = os.path.join(state_dir, "streak_ledger.db")
        if os.path.exists(ledger_db):
            # If existed beforehand, ensure it was not modified
            pass

    def test_cli_dryrun_with_valid_config(self):
        """Dry-run with valid config displays plan, exits 0, makes no network calls, creates no DB."""
        cfg_path = os.path.join(self.temp_dir, "valid_streak.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "session": "test_account",
                "message": "Daily streak test message",
                "targets": [
                    {"conv_id": "0:1:111:222", "conv_short_id": 7300000001, "conv_type": 1},
                    {"conv_id": "0:1:111:333", "conv_short_id": 7300000002, "conv_type": 1}
                ]
            }, f)

        proc = subprocess.run(
            [sys.executable, "-B", "main.py", "--config", cfg_path],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Plan (OFFLINE DRY-RUN)", proc.stdout)
        self.assertIn("Planned Targets (2 total):", proc.stdout)
        self.assertIn("0:1:111:222", proc.stdout)
        self.assertIn("0:1:111:333", proc.stdout)
        self.assertIn("Zero network calls were made.", proc.stdout)
        self.assertIn("No SQLite database files were created or modified.", proc.stdout)


class TestConfigValidation(unittest.TestCase):
    """Test configuration schema and validation constraints."""

    def test_empty_or_missing_session(self):
        ok, errs, _ = validate_config_data({"session": "", "message": "hi", "targets": [{"conv_id": "1", "conv_short_id": 1, "conv_type": 1}]})
        self.assertFalse(ok)
        self.assertIn("session", errs.lower())

    def test_empty_message(self):
        ok, errs, _ = validate_config_data({"session": "acc", "message": "  ", "targets": [{"conv_id": "1", "conv_short_id": 1, "conv_type": 1}]})
        self.assertFalse(ok)
        self.assertIn("message", errs.lower())

    def test_empty_targets(self):
        ok, errs, _ = validate_config_data({"session": "acc", "message": "hi", "targets": []})
        self.assertFalse(ok)
        self.assertIn("targets", errs.lower())

    def test_duplicate_target_conv_ids(self):
        ok, errs, _ = validate_config_data({
            "session": "acc",
            "message": "hi",
            "targets": [
                {"conv_id": "0:1:111:222", "conv_short_id": 1001, "conv_type": 1},
                {"conv_id": "0:1:111:222", "conv_short_id": 1002, "conv_type": 1},
            ]
        })
        self.assertFalse(ok)
        self.assertIn("duplicate", errs.lower())

    def test_non_positive_integer_short_id(self):
        # 0 is invalid
        ok, errs, _ = validate_config_data({
            "session": "acc", "message": "hi",
            "targets": [{"conv_id": "1", "conv_short_id": 0, "conv_type": 1}]
        })
        self.assertFalse(ok)
        self.assertIn("positive integer", errs.lower())

        # negative is invalid
        ok, errs, _ = validate_config_data({
            "session": "acc", "message": "hi",
            "targets": [{"conv_id": "1", "conv_short_id": -5, "conv_type": 1}]
        })
        self.assertFalse(ok)

        # string that cannot be int is invalid
        ok, errs, _ = validate_config_data({
            "session": "acc", "message": "hi",
            "targets": [{"conv_id": "1", "conv_short_id": "not_an_id", "conv_type": 1}]
        })
        self.assertFalse(ok)

    def test_invalid_conv_type(self):
        ok, errs, _ = validate_config_data({
            "session": "acc", "message": "hi",
            "targets": [{"conv_id": "1", "conv_short_id": 1234, "conv_type": 3}]
        })
        self.assertFalse(ok)
        self.assertIn("conv_type", errs.lower())

    def test_valid_config(self):
        ok, errs, targets = validate_config_data({
            "session": "acc",
            "message": "Daily streak message",
            "targets": [
                {"conv_id": "0:1:111:222", "conv_short_id": 7301234567, "conv_type": 1},
                {"conv_id": "0:1:111:333", "conv_short_id": 7307654321, "conv_type": 2},
            ]
        })
        self.assertTrue(ok)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]["conv_short_id"], 7301234567)
        self.assertEqual(targets[1]["conv_type"], 2)


class TestTimezoneAndDates(unittest.TestCase):
    """Test Asia/Ho_Chi_Minh date generation and midnight rollover."""

    def test_ho_chi_minh_date_format(self):
        date_str = ledger.get_current_ho_chi_minh_date()
        self.assertRegex(date_str, r"^\d{4}-\d{2}-\d{2}$")

    def test_midnight_rollover(self):
        # 23:59:59 on 2026-10-08 in UTC+7
        dt1 = datetime(2026, 10, 8, 23, 59, 59, tzinfo=ledger.TZ_HO_CHI_MINH)
        # 00:00:01 on 2026-10-09 in UTC+7
        dt2 = datetime(2026, 10, 9, 0, 0, 1, tzinfo=ledger.TZ_HO_CHI_MINH)
        self.assertEqual(ledger.get_current_ho_chi_minh_date(dt1), "2026-10-08")
        self.assertEqual(ledger.get_current_ho_chi_minh_date(dt2), "2026-10-09")


class TestPersistentLedgerDedupe(unittest.TestCase):
    """Test durable SQLite ledger deduplication, WAL synchronous=FULL, and readonly mode."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_ledger_")
        self.db_path = os.path.join(self.temp_dir, "test_ledger.db")
        initialize_fixture(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pragma_synchronous_is_full(self):
        conn = ledger._get_readwrite_connection(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA synchronous")
            val = cur.fetchone()[0]
            # In SQLite: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
            self.assertEqual(val, 2, "Expected synchronous=FULL (2)")
        finally:
            conn.close()

    def test_reserve_pending_durable_dedupe(self):
        uid = "user_12345"
        cid = "conv_999"
        date = "2026-10-08"
        uuid1 = "uuid-claim-1"

        # First reservation claims status pending
        res_date = reserve_at(uid, cid, uuid1, date=date, db_path=self.db_path)
        self.assertEqual(res_date, date)

        # Second reservation on same day must raise QuotaExceededError
        with self.assertRaises(ledger.QuotaExceededError):
            reserve_at(uid, cid, "uuid-claim-2", date=date, db_path=self.db_path)

        # V2 concurrency=1: a second conversation waits for the active transmission.
        with self.assertRaises(ledger.QuotaExceededError):
            reserve_at(uid, "conv_888", "uuid-blocked", date=date, db_path=self.db_path)
        ledger.confirm_send(uid, cid, date, uuid1, server_msg_id="10001", confirm_date=date, db_path=self.db_path)
        res2 = reserve_at(uid, "conv_888", "uuid-claim-3", date=date, db_path=self.db_path)
        self.assertEqual(res2, date)
        ledger.confirm_send(uid, "conv_888", date, "uuid-claim-3", server_msg_id="10002", confirm_date=date, db_path=self.db_path)

        # Different date on same conversation succeeds
        res3 = reserve_at(uid, cid, "uuid-claim-4", date="2026-10-09", db_path=self.db_path)
        self.assertEqual(res3, "2026-10-09")

    def test_persistent_dedupe_across_restart(self):
        uid = "user_12345"
        cid = "conv_999"
        date = "2026-10-08"

        reserve_at(uid, cid, "uuid-1", date=date, db_path=self.db_path)

        # Simulate complete process restart by querying fresh connection
        attempted, status = ledger.is_already_attempted(uid, cid, date=date, db_path=self.db_path)
        self.assertTrue(attempted)
        self.assertEqual(status, ledger.STATUS_PENDING)

        # Quota remains blocked
        with self.assertRaises(ledger.QuotaExceededError):
            reserve_at(uid, cid, "uuid-2", date=date, db_path=self.db_path)

    def test_concurrent_reservation_claims(self):
        uid = "user_concurrent"
        cid = "conv_race"
        date = "2026-10-08"

        success_count = 0
        quota_exceeded_count = 0

        def _claim_worker(idx):
            try:
                ledger.reserve_pending(uid, cid, f"uuid-worker-{idx}", date=date, db_path=self.db_path)
                return "success"
            except ledger.QuotaExceededError:
                return "quota_exceeded"

        with patch("ledger_requests.utc_now_ms", return_value=1791435600000), concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(_claim_worker, i) for i in range(8)]
            for f in concurrent.futures.as_completed(futures):
                res = f.result()
                if res == "success":
                    success_count += 1
                elif res == "quota_exceeded":
                    quota_exceeded_count += 1

        # Exactly ONE claim must succeed, all others must be blocked
        self.assertEqual(success_count, 1)
        self.assertEqual(quota_exceeded_count, 7)

    def test_readonly_mode_no_side_effects(self):
        missing_db = os.path.join(self.temp_dir, "missing_subdir", "nonexistent.db")
        # is_already_attempted on missing DB must return (False, None) without creating file or dir
        attempted, status = ledger.is_already_attempted("uid", "cid", db_path=missing_db)
        self.assertFalse(attempted)
        self.assertIsNone(status)
        self.assertFalse(os.path.exists(missing_db))
        self.assertFalse(os.path.exists(os.path.dirname(missing_db)))

        # On existing DB, readonly connection rejects writes
        reserve_at("uid", "cid", "uuid-1", date="2026-10-08", db_path=self.db_path)
        ro_conn = ledger._get_readonly_connection(self.db_path)
        self.assertIsNotNone(ro_conn)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro_conn.execute("INSERT INTO daily_ledger VALUES ('a','b','c','d','e','f','g','h','i')")
        finally:
            ro_conn.close()


class TestLedgerConfirmAndFailure(unittest.TestCase):
    """Test confirm_send, mark_failed_unknown, crossover handling, and RunLock."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_ledger2_")
        self.db_path = os.path.join(self.temp_dir, "test_ledger.db")
        initialize_fixture(self.db_path)
        self.lock_path = os.path.join(self.temp_dir, "run.lock")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_confirm_send_validation(self):
        uid = "user_1"
        cid = "conv_1"
        date = "2026-10-08"
        client_uuid = "client-uuid-123"

        # Cannot confirm without reservation
        with self.assertRaises(ledger.LedgerError):
            ledger.confirm_send(uid, cid, date, client_uuid, server_msg_id="10001", db_path=self.db_path)

        # Reserve
        reserve_at(uid, cid, client_uuid, date=date, db_path=self.db_path)

        # UUID mismatch rejected
        with self.assertRaises(ledger.LedgerError):
            ledger.confirm_send(uid, cid, date, "wrong-uuid", server_msg_id="10001", db_path=self.db_path)

        # Non-positive server_msg_id rejected
        with self.assertRaises(ValueError):
            ledger.confirm_send(uid, cid, date, client_uuid, server_msg_id="0", db_path=self.db_path)
        with self.assertRaises(ValueError):
            ledger.confirm_send(uid, cid, date, client_uuid, server_msg_id="-5", db_path=self.db_path)

        # Valid confirmation succeeds
        ledger.confirm_send(uid, cid, date, client_uuid, server_msg_id="7355123456", db_path=self.db_path)

        attempted, status = ledger.is_already_attempted(uid, cid, date=date, db_path=self.db_path)
        self.assertTrue(attempted)
        self.assertEqual(status, ledger.STATUS_CONFIRMED)

    def test_midnight_crossover_confirmation_preserves_records(self):
        uid = "user_crossover"
        cid = "conv_1"
        send_date = "2026-10-08"
        confirm_date = "2026-10-09"
        client_uuid = "uuid-cross"

        reserve_at(uid, cid, client_uuid, date=send_date, db_path=self.db_path)
        ledger.confirm_send(
            uid, cid, send_date, client_uuid, server_msg_id="999",
            confirm_date=confirm_date, db_path=self.db_path
        )

        # Send date is confirmed
        att1, st1 = ledger.is_already_attempted(uid, cid, date=send_date, db_path=self.db_path)
        self.assertEqual(st1, ledger.STATUS_CONFIRMED)

        # Confirm date is conservatively blocked
        att2, st2 = ledger.is_already_attempted(uid, cid, date=confirm_date, db_path=self.db_path)
        self.assertEqual(st2, ledger.STATUS_BLOCKED_CROSSOVER)

    def test_mark_failed_unknown_blocks_retry_and_never_overwrites_confirmed(self):
        uid = "user_fail"
        cid = "conv_fail"
        date = "2026-10-08"
        client_uuid = "uuid-fail"

        reserve_at(uid, cid, client_uuid, date=date, db_path=self.db_path)
        ledger.mark_failed_unknown(uid, cid, date, client_uuid, reason_code=ledger.REASON_TIMEOUT, db_path=self.db_path)

        # Status is failed_unknown
        _, st = ledger.is_already_attempted(uid, cid, date=date, db_path=self.db_path)
        self.assertEqual(st, ledger.STATUS_FAILED_UNKNOWN)

        # No retry allowed today
        with self.assertRaises(ledger.QuotaExceededError):
            reserve_at(uid, cid, "uuid-retry", date=date, db_path=self.db_path)

        # Now test that confirmed record cannot be overwritten by failure
        uid2 = "user_ok"
        cid2 = "conv_ok"
        uuid2 = "uuid-ok"
        reserve_at(uid2, cid2, uuid2, date=date, db_path=self.db_path)
        ledger.confirm_send(uid2, cid2, date, uuid2, server_msg_id="111", db_path=self.db_path)

        ledger.mark_failed_unknown(uid2, cid2, date, uuid2, reason_code=ledger.REASON_DISCONNECTED, db_path=self.db_path)
        _, st2 = ledger.is_already_attempted(uid2, cid2, date=date, db_path=self.db_path)
        self.assertEqual(st2, ledger.STATUS_CONFIRMED)

    def test_process_run_lock(self):
        lock1 = ledger.RunLock(self.lock_path)
        lock1.acquire()
        try:
            lock2 = ledger.RunLock(self.lock_path)
            with self.assertRaises(ledger.LockError):
                lock2.acquire()
        finally:
            lock1.release()

        # After release, lock can be acquired again
        lock3 = ledger.RunLock(self.lock_path)
        lock3.acquire()
        lock3.release()


class TestEchoCorrelation(unittest.TestCase):
    """Test strict correlated server message echo confirmation."""

    def test_true_echo_matches(self):
        candidate = {
            "conv_id": "0:1:111:222",
            "sender_id": "111",
            "server_message_id": 7355000001,
            "text": "Daily streak message",
            "client_message_id": "test-uuid-abc",
        }
        matched, reason, sid = LttkClient.check_echo_correlation(
            candidate,
            expected_client_msg_id="test-uuid-abc",
            expected_conv_id="0:1:111:222",
            own_user_id="111",
            expected_text="Daily streak message",
        )
        self.assertTrue(matched)
        self.assertEqual(sid, 7355000001)

    def test_unrelated_uuid_rejected(self):
        candidate = {
            "conv_id": "0:1:111:222",
            "sender_id": "111",
            "server_message_id": 7355000001,
            "text": "Daily streak message",
            "client_message_id": "other-uuid-xyz",
        }
        matched, reason, _ = LttkClient.check_echo_correlation(
            candidate,
            expected_client_msg_id="test-uuid-abc",
            expected_conv_id="0:1:111:222",
            own_user_id="111",
            expected_text="Daily streak message",
        )
        self.assertFalse(matched)
        self.assertEqual(reason, "client_id_mismatch")

    def test_other_sender_rejected(self):
        candidate = {
            "conv_id": "0:1:111:222",
            "sender_id": "222",  # Partner sender, not own UID
            "server_message_id": 7355000001,
            "text": "Daily streak message",
            "client_message_id": "test-uuid-abc",
        }
        matched, reason, _ = LttkClient.check_echo_correlation(
            candidate,
            expected_client_msg_id="test-uuid-abc",
            expected_conv_id="0:1:111:222",
            own_user_id="111",
            expected_text="Daily streak message",
        )
        self.assertFalse(matched)
        self.assertEqual(reason, "sender_not_own_uid")

    def test_wrong_conv_id_rejected(self):
        candidate = {
            "conv_id": "0:1:111:333",
            "sender_id": "111",
            "server_message_id": 7355000001,
            "text": "Daily streak message",
            "client_message_id": "test-uuid-abc",
        }
        matched, reason, _ = LttkClient.check_echo_correlation(
            candidate,
            expected_client_msg_id="test-uuid-abc",
            expected_conv_id="0:1:111:222",
            own_user_id="111",
            expected_text="Daily streak message",
        )
        self.assertFalse(matched)
        self.assertEqual(reason, "conv_id_mismatch")

    def test_text_mismatch_rejected(self):
        candidate = {
            "conv_id": "0:1:111:222",
            "sender_id": "111",
            "server_message_id": 7355000001,
            "text": "Unrelated incoming text",
            "client_message_id": "test-uuid-abc",
        }
        matched, reason, _ = LttkClient.check_echo_correlation(
            candidate,
            expected_client_msg_id="test-uuid-abc",
            expected_conv_id="0:1:111:222",
            own_user_id="111",
            expected_text="Daily streak message",
        )
        self.assertFalse(matched)
        self.assertEqual(reason, "content_text_mismatch")

    def test_candidate_isolation_no_raw_frame_bleeding(self):
        """Ensure candidates from multi-message frames are tested individually."""
        candidates = [
            {
                "conv_id": "0:1:111:222",
                "sender_id": "222",
                "server_message_id": 100,
                "text": "sibling message",
                "client_message_id": "sibling-uuid",
            },
            {
                "conv_id": "0:1:111:222",
                "sender_id": "111",
                "server_message_id": 101,
                "text": "my echo",
                "client_message_id": "my-uuid",
            }
        ]
        # Candidate 0 must fail
        ok0, _, _ = LttkClient.check_echo_correlation(candidates[0], "my-uuid", "0:1:111:222", "111", "my echo")
        self.assertFalse(ok0)

        # Candidate 1 must succeed
        ok1, _, sid = LttkClient.check_echo_correlation(candidates[1], "my-uuid", "0:1:111:222", "111", "my echo")
        self.assertTrue(ok1)
        self.assertEqual(sid, 101)


class TestProtobufGenerationAndParsing(unittest.TestCase):
    """Test that protobuf uses explicit conv_short_id and conv_type, and parses fields accurately."""

    def test_build_msg_body_uses_explicit_ids(self):
        cid = "0:1:111:222"
        short_id = 730999888777
        client_uuid = "client-uuid-001"
        text = "Hello world"

        body = _build_msg_body(
            conv_id=cid,
            short_id=short_id,
            text=text,
            client_id=client_uuid,
            conv_type=1,
        )

        # Field 2 must be conv_type (1)
        # Field 3 must be short_id (730999888777), NOT hardcoded 7305620471088775430
        expected_f2 = f_varint(2, 1)
        expected_f3 = f_varint(3, short_id)
        hardcoded_f3 = f_varint(3, 7305620471088775430)

        self.assertIn(expected_f2, body)
        self.assertIn(expected_f3, body)
        self.assertNotIn(hardcoded_f3, body)

    def test_parse_msgbody_fields(self):
        """Construct synthetic incoming MessageBody and verify parsed fields."""
        conv_id = "0:1:111:222"
        conv_type = 1
        server_msg_id = 7355112233
        index_in_conv = 42
        conv_short_id = 730999888777
        msg_type = 7
        sender_id = 111
        content_json = json.dumps({"aweType": 0, "text": "Test echo message"})
        client_uuid = "test-uuid-ext-999"

        ext_bytes = f_bytes(1, "s:client_message_id".encode()) + f_bytes(2, client_uuid.encode())

        data = (
            f_str(1, conv_id) +
            f_varint(2, conv_type) +
            f_varint(3, server_msg_id) +
            f_varint(4, index_in_conv) +
            f_varint(5, conv_short_id) +
            f_varint(6, msg_type) +
            f_varint(7, sender_id) +
            f_str(8, content_json) +
            f_bytes(9, ext_bytes)
        )

        parsed = LttkClient._parse_msgbody(data)

        self.assertEqual(parsed.get("conv_id"), conv_id)
        self.assertEqual(parsed.get("conv_type"), conv_type)
        self.assertEqual(parsed.get("server_message_id"), server_msg_id)
        self.assertEqual(parsed.get("msg_id"), server_msg_id)
        self.assertEqual(parsed.get("index_in_conversation"), index_in_conv)
        self.assertEqual(parsed.get("conv_short_id"), conv_short_id)
        self.assertEqual(parsed.get("sender_id"), str(sender_id))
        self.assertEqual(parsed.get("text"), "Test echo message")
        self.assertEqual(parsed.get("client_message_id"), client_uuid)


class TestSecurityAndRedaction(unittest.TestCase):
    """Test sink redaction, session traversal prevention, and plugin isolation."""

    def test_log_sink_redaction(self):
        raw_token = "msToken=abcdef1234567890xyz"
        redacted = log.redact(raw_token)
        self.assertNotIn("abcdef1234567890xyz", redacted)
        self.assertIn("[REDACTED]", redacted)

        raw_cookie = "Cookie: sessionid=secret_session_token_123"
        redacted2 = log.redact(raw_cookie)
        self.assertNotIn("secret_session_token_123", redacted2)
        self.assertIn("[REDACTED]", redacted2)

    def test_session_name_traversal_rejection(self):
        import qrlogin
        with self.assertRaises(ValueError):
            qrlogin._validate_session_name("../../evil")
        with self.assertRaises(ValueError):
            qrlogin._validate_session_name("..\\evil")
        with self.assertRaises(ValueError):
            qrlogin._validate_session_name("/etc/passwd")

    def test_videodl_external_upload_disabled(self):
        import plugins.videodl as videodl
        self.assertFalse(videodl.ENABLED)
        with self.assertRaises(NotImplementedError):
            videodl._upload(b"fake_data", "test.mp4")

    def test_tls_verification_configuration(self):
        ctx = ssl.create_default_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)


class MockWebSocket:
    def __init__(self, incoming_frames=None, on_send=None, send_exc=None):
        self.sent_packets = []
        self.incoming_frames = list(incoming_frames or [])
        self.on_send = on_send
        self.send_exc = send_exc

    async def send(self, data):
        if self.send_exc:
            raise self.send_exc
        self.sent_packets.append(data)
        if self.on_send:
            self.on_send(data)

    async def recv(self):
        if not self.incoming_frames:
            raise asyncio.TimeoutError()
        frame = self.incoming_frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame


def _build_synthetic_echo_frame(
    conv_id: str,
    sender_id: str,
    client_msg_id: str,
    text: str,
    server_msg_id: int = 7355123456,
) -> bytes:
    body = (
        f_str(1, conv_id)
        + f_varint(2, 1)
        + f_varint(3, server_msg_id)
        + f_varint(4, 42)
        + f_varint(5, 8888)
        + f_varint(6, 7)
        + f_varint(7, int(sender_id))
        + f_str(8, json.dumps({"text": text}))
        + f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, client_msg_id))
    )
    return f_bytes(1, body)


def _build_synthetic_lz4_frame(data: bytes) -> bytes:
    import lz4.block
    comp = lz4.block.compress(data, store_size=False)
    return b"header__lz4\x0a" + encode_varint(0) + b"\x12" + encode_varint(len(comp)) + comp


class TestFakeWebSocketScenarios(unittest.IsolatedAsyncioTestCase):
    """Fake WebSocket unit tests covering transmission, correlation, and recovery scenarios."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_fake_ws_")
        self.db_path = os.path.join(self.temp_dir, "test_ledger.db")
        initialize_fixture(self.db_path)
        self.target = {
            "conv_id": "0:1:111:222",
            "conv_short_id": 7300000001,
            "conv_type": 1,
        }
        self.uid = "111"
        self.fixed_uuid = "uuid-fixed-test-1234"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_fake_ws_confirmed_echo(self):
        """Correlated server echo matching client UUID, conv ID, UID, text, and positive sid records success."""
        echo = _build_synthetic_echo_frame(
            conv_id=self.target["conv_id"],
            sender_id=self.uid,
            client_msg_id=self.fixed_uuid,
            text="Streak hello",
            server_msg_id=7355123456,
        )
        ws = MockWebSocket(incoming_frames=[echo])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res = await _send_target_oneshot(
                ws, self.uid, self.target, "Streak hello", timeout_seconds=1.0, db_path=self.db_path
            )

        self.assertEqual(res["status"], ledger.STATUS_CONFIRMED)
        self.assertEqual(res["server_msg_id"], "7355123456")
        self.assertEqual(len(ws.sent_packets), 1)

        # Confirm recorded in ledger
        today = ledger.get_current_ho_chi_minh_date()
        att, st = ledger.is_already_attempted(self.uid, self.target["conv_id"], date=today, db_path=self.db_path)
        self.assertTrue(att)
        self.assertEqual(st, ledger.STATUS_CONFIRMED)

    async def test_fake_ws_ack_only_not_confirmed(self):
        """Generic server ACK frame without message body echo must NOT confirm send."""
        ack_frame = b'{"status": 0, "ack": 1, "seq": 999}'
        ws = MockWebSocket(incoming_frames=[ack_frame])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res = await _send_target_oneshot(
                ws, self.uid, self.target, "Streak hello", timeout_seconds=0.2, db_path=self.db_path
            )

        self.assertEqual(res["status"], ledger.STATUS_FAILED_UNKNOWN)
        today = ledger.get_current_ho_chi_minh_date()
        att, st = ledger.is_already_attempted(self.uid, self.target["conv_id"], date=today, db_path=self.db_path)
        self.assertTrue(att)
        self.assertEqual(st, ledger.STATUS_FAILED_UNKNOWN)

    async def test_fake_ws_wrong_sender_or_uuid(self):
        """Candidate echo with partner sender UID or mismatched UUID is rejected."""
        # Case A: Wrong sender ID (partner UID 222 instead of own UID 111)
        wrong_sender = _build_synthetic_echo_frame(
            conv_id=self.target["conv_id"],
            sender_id="222",
            client_msg_id=self.fixed_uuid,
            text="Streak hello",
        )
        ws1 = MockWebSocket(incoming_frames=[wrong_sender])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res1 = await _send_target_oneshot(
                ws1, self.uid, self.target, "Streak hello", timeout_seconds=0.2, db_path=self.db_path
            )
        self.assertEqual(res1["status"], ledger.STATUS_FAILED_UNKNOWN)

        # Clear DB for Case B
        os.remove(self.db_path)
        initialize_fixture(self.db_path)

        # Case B: Wrong UUID (incoming UUID doesn't match client_msg_id)
        wrong_uuid = _build_synthetic_echo_frame(
            conv_id=self.target["conv_id"],
            sender_id=self.uid,
            client_msg_id="completely-unrelated-uuid",
            text="Streak hello",
        )
        ws2 = MockWebSocket(incoming_frames=[wrong_uuid])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res2 = await _send_target_oneshot(
                ws2, self.uid, self.target, "Streak hello", timeout_seconds=0.2, db_path=self.db_path
            )
        self.assertEqual(res2["status"], ledger.STATUS_FAILED_UNKNOWN)

    async def test_fake_ws_timeout_and_error(self):
        """Monotonic timeout on recv or send error marks failed_unknown without retry."""
        # Recv timeout
        ws_timeout = MockWebSocket(incoming_frames=[])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res = await _send_target_oneshot(
                ws_timeout, self.uid, self.target, "Streak hello", timeout_seconds=0.1, db_path=self.db_path
            )
        self.assertEqual(res["status"], ledger.STATUS_FAILED_UNKNOWN)
        self.assertEqual(res["reason"], ledger.REASON_CORRELATION_TIMEOUT)

        # Send error
        os.remove(self.db_path)
        initialize_fixture(self.db_path)
        ws_error = MockWebSocket(send_exc=ConnectionResetError("Socket broken"))
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res2 = await _send_target_oneshot(
                ws_error, self.uid, self.target, "Streak hello", timeout_seconds=1.0, db_path=self.db_path
            )
        self.assertEqual(res2["status"], ledger.STATUS_FAILED_UNKNOWN)
        self.assertEqual(res2["reason"], ledger.REASON_WS_ERROR)

    async def test_fake_ws_repeat_restart_no_duplicate(self):
        """Repeated call after confirmation or failure skips immediately and never sends twice."""
        echo = _build_synthetic_echo_frame(
            conv_id=self.target["conv_id"],
            sender_id=self.uid,
            client_msg_id=self.fixed_uuid,
            text="Streak hello",
        )
        ws1 = MockWebSocket(incoming_frames=[echo])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res1 = await _send_target_oneshot(
                ws1, self.uid, self.target, "Streak hello", timeout_seconds=1.0, db_path=self.db_path
            )
        self.assertEqual(res1["status"], ledger.STATUS_CONFIRMED)
        self.assertEqual(len(ws1.sent_packets), 1)

        # Second attempt on same day: must skip without sending
        ws2 = MockWebSocket(incoming_frames=[])
        res2 = await _send_target_oneshot(
            ws2, self.uid, self.target, "Streak hello", timeout_seconds=1.0, db_path=self.db_path
        )
        self.assertEqual(res2["status"], "skipped")
        self.assertEqual(res2["reason"], "already_confirmed")
        self.assertEqual(len(ws2.sent_packets), 0)

    async def test_fake_ws_malformed_echo(self):
        """Malformed protobuf wire framing in echo is safely rejected and does not confirm."""
        # Truncated / corrupt bytes
        corrupted_bytes = b"\x0a\x80\x80\x80\x80\x10\x08\x99"
        ws = MockWebSocket(incoming_frames=[corrupted_bytes])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res = await _send_target_oneshot(
                ws, self.uid, self.target, "Streak hello", timeout_seconds=0.2, db_path=self.db_path
            )
        self.assertEqual(res["status"], ledger.STATUS_FAILED_UNKNOWN)

    async def test_fake_ws_compressed_errors(self):
        """Unverified JSON error text does not imply restriction; correlation times out."""
        ban_payload = b'{"error": "sending_ban", "error_code": 1001, "msg": "account restricted"}'
        lz4_frame = _build_synthetic_lz4_frame(ban_payload)
        self.assertEqual(LttkClient._decompress_lz4_frame(lz4_frame), ban_payload)
        ws = MockWebSocket(incoming_frames=[lz4_frame])
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            res = await _send_target_oneshot(
                ws, self.uid, self.target, "Streak hello", timeout_seconds=1.0, db_path=self.db_path
            )
        self.assertEqual(res["status"], ledger.STATUS_FAILED_UNKNOWN)
        self.assertEqual(res["reason"], ledger.REASON_CORRELATION_TIMEOUT)

    async def test_fake_ws_durable_pending_visible_inside_send(self):
        """Pending reservation is committed and visible in database before ws.send completes."""
        today = ledger.get_current_ho_chi_minh_date()
        reservation_verified_in_flight = False

        def _verify_db_during_send(_packet):
            nonlocal reservation_verified_in_flight
            att, st = ledger.is_already_attempted(self.uid, self.target["conv_id"], date=today, db_path=self.db_path)
            if att and st == ledger.STATUS_PENDING:
                reservation_verified_in_flight = True

        ws = MockWebSocket(on_send=_verify_db_during_send)
        with patch("uuid.uuid4", return_value=self.fixed_uuid):
            await _send_target_oneshot(
                ws, self.uid, self.target, "Streak hello", timeout_seconds=0.1, db_path=self.db_path
            )

        self.assertTrue(reservation_verified_in_flight, "Durable reservation must be visible in SQLite during ws.send")

    async def test_fake_ws_crash_pending_blocks_across_days(self):
        """Crash-left pending reservation from previous day conservatively blocks sending on next day."""
        yesterday = "2026-10-07"
        today = "2026-10-08"

        # Simulate crash yesterday: left in 'pending' status
        reserve_at(self.uid, self.target["conv_id"], "crashed-yesterday-uuid", date=yesterday, db_path=self.db_path)

        ws = MockWebSocket()
        with patch("ledger.get_current_ho_chi_minh_date", return_value=today):
            res = await _send_target_oneshot(
                ws, self.uid, self.target, "Streak hello", timeout_seconds=1.0, db_path=self.db_path
            )

        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["reason"], "quota_claimed")
        self.assertEqual(len(ws.sent_packets), 0, "No network send should occur when crash-left pending is active")


if __name__ == "__main__":
    unittest.main()
