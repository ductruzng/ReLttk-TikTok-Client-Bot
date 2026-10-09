"""Reviewer regressions; no accounts, credentials, or external network."""
import contextlib
import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
import oneshot
import ledger
import log
import qrlogin
from client import LttkClient
from core.proto import f_str, f_varint, f_bytes


class IndependentReview(unittest.TestCase):
    def test_session_read_fails_before_open_when_chmod_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "fake.json"
            session.write_text('{"sessionid":"fake-only"}', encoding="utf-8")
            with patch.object(qrlogin, "_get_session_path", return_value=str(session)), \
                    patch.object(qrlogin.sys, "platform", "linux"), \
                    patch.object(qrlogin.os, "chmod", side_effect=PermissionError("denied")), \
                    patch.object(qrlogin.os, "open") as opened:
                with self.assertRaises(PermissionError):
                    qrlogin.load_session("fake")
                opened.assert_not_called()

    def test_failed_session_write_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(qrlogin, "_SESSION_DIR", tmp):
            with patch.object(qrlogin.os, "write", return_value=0):
                with self.assertRaises(OSError):
                    qrlogin._write_cookies({"sessionid": "fake-only"}, "fake")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_conflicting_modes_never_enter_network_command(self):
        for args in (["--send", "--dry-run"], ["--dry-run", "login"], ["--send", "login"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["main.py", *args]), \
                    patch.object(main, "cmd_send") as send, patch.object(main, "cmd_login") as login, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertNotEqual(caught.exception.code, 0)
                send.assert_not_called()
                login.assert_not_called()

    def test_pending_survives_restart_and_next_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state" / "ledger.db")
            ledger.reserve_pending("111", "0:1:111:222", "fake-uuid", date="2026-10-08", db_path=path)
            with self.assertRaises(ledger.QuotaExceededError):
                ledger.reserve_pending("111", "0:1:111:222", "different-uuid", date="2026-10-09", db_path=path)

    def test_malformed_echo_is_not_confirmation(self):
        body = (f_str(1, "0:1:111:222") + f_varint(2, 1) + f_varint(3, 1234)
                + f_varint(4, 42) + f_varint(5, 8888) + f_varint(6, 7)
                + f_varint(7, 111) + f_str(8, '{"text":"hi"}')
                + f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "fake-uuid")))
        good = LttkClient.find_all_msgbodies(f_bytes(1, body))
        self.assertTrue(any(LttkClient.check_echo_correlation(
            candidate, expected_client_msg_id="fake-uuid", expected_conv_id="0:1:111:222",
            own_user_id="111", expected_text="hi")[0] for candidate in good))
        for suffix in (b"\x80", b"\x52\x7fshort", f_varint(7, 222), f_varint(3, 5678), b"\x00"):
            with self.subTest(suffix=suffix):
                candidates = LttkClient.find_all_msgbodies(f_bytes(1, body + suffix))
                matches = [LttkClient.check_echo_correlation(
                    candidate, expected_client_msg_id="fake-uuid", expected_conv_id="0:1:111:222",
                    own_user_id="111", expected_text="hi")[0] for candidate in candidates]
                self.assertFalse(any(matches))

        bad_ext = f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "fake-uuid") + b"\x80")
        candidates = LttkClient.find_all_msgbodies(f_bytes(1, body + bad_ext))
        self.assertFalse(any(LttkClient.check_echo_correlation(
            candidate, expected_client_msg_id="fake-uuid", expected_conv_id="0:1:111:222",
            own_user_id="111", expected_text="hi")[0] for candidate in candidates))

    def test_strict_wire_regressions(self):
        body = (f_str(1, "0:1:111:222") + f_varint(2, 1) + f_varint(3, 1234)
                + f_varint(4, 42) + f_varint(5, 8888) + f_varint(6, 7)
                + f_varint(7, 111) + f_str(8, '{"text":"hi"}')
                + f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "fake-uuid")))

        # 1. Conflicting UUID ext entries reject candidate
        conflicting_ext = f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "other-uuid"))
        cands = LttkClient.find_all_msgbodies(f_bytes(1, body + conflicting_ext))
        self.assertFalse(any(LttkClient.check_echo_correlation(
            c, expected_client_msg_id="fake-uuid", expected_conv_id="0:1:111:222",
            own_user_id="111", expected_text="hi")[0] for c in cands))

        # 2. Malformed map entries reject candidate
        bad_maps = [
            f_bytes(9, f_bytes(1, b"\xff\xff") + f_str(2, "fake-uuid")),  # invalid UTF-8 key
            f_bytes(9, f_str(1, "s:client_message_id") + f_bytes(2, b"\xff\xff")),  # invalid UTF-8 value
            f_bytes(9, f_str(1, "k1") + f_str(1, "k2") + f_str(2, "fake-uuid")),  # conflicting duplicate map key
            f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "u1") + f_str(2, "u2")),  # conflicting duplicate map value
            f_bytes(9, b"\x00"),  # field 0 in map
        ]
        for bm in bad_maps:
            with self.subTest(bad_map=bm):
                cands = LttkClient.find_all_msgbodies(f_bytes(1, body + bm))
                self.assertFalse(any(LttkClient.check_echo_correlation(
                    c, expected_client_msg_id="fake-uuid", expected_conv_id="0:1:111:222",
                    own_user_id="111", expected_text="hi")[0] for c in cands))

        # 3. Malformed outer envelope fails closed (returns empty list)
        for bad_env in (
            f_bytes(1, body) + b"\x80",
            f_bytes(1, body) + b"\x00",
            f_bytes(1, body) + b"\x52\x7fshort",
        ):
            with self.subTest(bad_env=bad_env):
                self.assertEqual(LttkClient.find_all_msgbodies(bad_env), [])

        # 4. Valid sibling MessageBody preserved alongside arbitrary string payload
        sibling_body = (f_str(1, "0:1:111:222") + f_varint(2, 1) + f_varint(3, 5678)
                        + f_varint(4, 43) + f_varint(5, 8888) + f_varint(6, 7)
                        + f_varint(7, 111) + f_str(8, '{"text":"sibling"}')
                        + f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "sibling-uuid")))
        multi_frame = f_bytes(1, body) + f_bytes(2, b"arbitrary plain text payload") + f_bytes(3, sibling_body)
        cands = LttkClient.find_all_msgbodies(multi_frame)
        self.assertEqual(len(cands), 2)

    def test_redactor_covers_cookie_variants_and_qr_queries(self):
        for raw, secret in [
            ("{'sessionid_ss': 'sensitive-cookie'}", "sensitive-cookie"),
            ("token=x", "=x"),
            ("https://www.tiktok.com/t/qr?ticket=secretqrvalue", "secretqrvalue"),
            ("wss://example.invalid/ws?unknown=secretwsvalue", "secretwsvalue"),
            ("{'Cookie': 'arbitrary=secretcookievalue'}", "secretcookievalue"),
        ]:
            with self.subTest(raw=raw):
                self.assertNotIn(secret, log.redact(raw))

    def test_valid_dry_run_has_no_network_credential_or_state_access(self):
        # Audit hooks reject even attempted access, including errors swallowed by application code.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "plan.json"
            cfg.write_text(json.dumps({"session": "fake_account", "message": "hi", "targets": [
                {"conv_id": "0:1:111:222", "conv_short_id": 8888, "conv_type": 1}]}), encoding="utf-8")
            script = '''
import os, runpy, sys
sys.path.insert(0, sys.argv[1])
entry = os.path.join(sys.argv[1], "main.py")
config = sys.argv[2]
violations = []
def audit(event, args):
    bad = event.startswith("socket.") or event in {"sqlite3.connect", "os.mkdir", "os.remove", "os.rename", "os.chmod", "os.rmdir"}
    if event == "open":
        path, mode, flags = args
        bad = bad or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if isinstance(path, (str, bytes)):
            name = os.fsdecode(path).replace(chr(92), "/").lower()
            bad = bad or "/sesion/" in name or name.endswith("cookies.json") or "/state/" in name
    if bad:
        violations.append(event)
        raise RuntimeError("forbidden offline access")
sys.argv = [entry, "--config", config]
sys.addaudithook(audit)
try:
    runpy.run_path(entry, run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, exc.code
assert not violations, violations
'''
            result = subprocess.run([sys.executable, "-B", "-S", "-c", script, str(ROOT), str(cfg)],
                                    capture_output=True, text=True, timeout=20, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class IndependentSendReview(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_dispatch_does_not_accept_strangers_by_default(self):
        bot = LttkClient()
        bot._own_user_id = "111"
        with patch("client.accept_stranger", return_value=False) as accept:
            await bot._dispatch({"conv_id": "0:1:111:222", "sender_id": "222"})
        accept.assert_not_called()

    async def exercise(self, echo_sender=None, transmit_error=False):
        target = {"conv_id": "0:1:111:222", "conv_short_id": 8888, "conv_type": 1}
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state" / "ledger.db")
            frames = []
            if echo_sender is not None:
                body = (f_str(1, target["conv_id"]) + f_varint(2, 1) + f_varint(3, 1234)
                        + f_varint(4, 42) + f_varint(5, 8888) + f_varint(6, 7)
                        + f_varint(7, echo_sender) + f_str(8, '{"text":"hi"}')
                        + f_bytes(9, f_str(1, "s:client_message_id") + f_str(2, "fake-uuid")))
                frames.append(f_bytes(1, body))
            review = self

            class FakeSocket:
                sends = 0

                async def send(self, packet):
                    self.sends += 1
                    # Independent connection verifies reservation was committed before transmission.
                    attempted, status = ledger.is_already_attempted("111", target["conv_id"], date="2026-10-08", db_path=db)
                    review.assertTrue(attempted)
                    review.assertEqual(status, ledger.STATUS_PENDING)
                    if transmit_error:
                        raise RuntimeError("sessionid_ss=never-log-this-value")

                async def recv(self):
                    if frames:
                        return frames.pop(0)
                    raise asyncio.TimeoutError()

            ws = FakeSocket()
            output = io.StringIO()
            with patch.object(oneshot.uuid, "uuid4", return_value="fake-uuid"), \
                    patch.object(ledger, "get_current_ho_chi_minh_date", return_value="2026-10-08"), \
                    contextlib.redirect_stdout(output):
                result = await oneshot._send_target_oneshot(ws, "111", target, "hi", timeout_seconds=0.1, db_path=db)
                repeat = await oneshot._send_target_oneshot(ws, "111", target, "hi", timeout_seconds=0.1, db_path=db)
            self.assertEqual(ws.sends, 1)
            self.assertEqual(repeat["status"], "skipped")
            self.assertNotIn("never-log-this-value", output.getvalue())
            expected = ledger.STATUS_CONFIRMED if echo_sender == 111 and not transmit_error else ledger.STATUS_FAILED_UNKNOWN
            self.assertEqual(result["status"], expected)
            attempted, status = ledger.is_already_attempted("111", target["conv_id"], date="2026-10-08", db_path=db)
            self.assertEqual(status, expected)

    async def test_matching_echo_commits_success_once(self):
        await self.exercise(echo_sender=111)

    async def test_other_sender_never_confirms(self):
        await self.exercise(echo_sender=222)

    async def test_send_completion_without_echo_is_unknown(self):
        await self.exercise()

    async def test_transmit_error_is_unknown_and_sanitized(self):
        await self.exercise(transmit_error=True)


if __name__ == "__main__":
    unittest.main()
