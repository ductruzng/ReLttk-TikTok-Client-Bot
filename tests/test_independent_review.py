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
from ledger_fixtures import reserve_at, initialize_fixture
import log
import qrlogin
from client import LttkClient
from core.proto import f_str, f_varint, f_bytes


class IndependentReview(unittest.TestCase):
    def setUp(self):
        # Never pick up a real browser ttwid/access_key from the developer's ws_auth.local.json
        p = patch.object(oneshot, "load_ws_auth", return_value={"ttwid": "1|fake-secret-ttwid", "access_key": "c" * 32})
        p.start()
        self.addCleanup(p.stop)

    def test_websocket_rejection_reports_only_http_status(self):
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response

        for status in (401, 403, 429):
            response = Response(status, "fake-sensitive-reason", Headers({
                "Set-Cookie": "sessionid=fake-sensitive-cookie"
            }), body=b"fake-sensitive-body")
            output = io.StringIO()
            with self.subTest(status=status), \
                    patch.object(oneshot, "run_oneshot_send", side_effect=InvalidStatus(response)), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(main.cmd_send("unused-test-plan.json"), 1)
            self.assertIn(f"HTTP {status}", output.getvalue())
            self.assertIn("No message was transmitted", output.getvalue())
            self.assertNotIn("fake-sensitive", output.getvalue())

    def test_ws_url_matches_browser_handshake_shape(self):
        from client import build_ws_url
        url = build_ws_url({"ttwid": "1%7Cfake%7C123%7Cabc", "msToken": "fake_tok", "sessionid": "fake-sid"},
                           access_key="0" * 32)
        self.assertEqual(
            url,
            "wss://im-ws-sg.tiktok.com/ws/v2?device_platform=web&version_code=fws_1.0.0"
            f"&access_key={'0' * 32}&fpid=9&aid=1459&ttwid=1|fake|123|abc&xsack=1&xaack=1&xsqos=0",
        )
        self.assertNotIn("fake-sid", url)
        self.assertNotIn("fake_tok", url)
        self.assertIn("access_key=277f7a051d7a540326780c413dbc2b9c", build_ws_url({}))
        self.assertNotIn("ttwid=", build_ws_url({}))

    def test_ws_auth_file_pair_is_validated_and_overrides_session_ttwid(self):
        from client import apply_ws_auth, load_ws_auth
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ws_auth.local.json"
            self.assertEqual(load_ws_auth(str(path)), {})
            path.write_text(json.dumps({"ttwid": "1%7Cfake-Id_1%7C123%7Cabc", "access_key": "AB" * 16}), encoding="utf-8")
            auth = load_ws_auth(str(path))
            self.assertEqual(auth, {"ttwid": "1|fake-Id_1|123|abc", "access_key": "ab" * 16})
            for bad in ({"ttwid": "x", "access_key": "ab" * 16}, {"ttwid": "1|a|1|b", "access_key": "zz"}, []):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    with self.assertRaises(ValueError) as caught:
                        load_ws_auth(str(path))
                    self.assertNotIn("ab" * 16, str(caught.exception))
        session = {"sessionid": "fake-sid", "ttwid": "1|session|1|aa"}
        self.assertEqual(apply_ws_auth(session, auth)["ttwid"], "1|fake-Id_1|123|abc")
        self.assertEqual(session["ttwid"], "1|session|1|aa")
        self.assertIs(apply_ws_auth(session, {}), session)

    def test_ws_handshake_rejection_logs_safe_diagnostics_and_sends_nothing(self):
        from unittest.mock import AsyncMock
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response

        cookies = {"sessionid": "fake-secret-sid", "msToken": "fake-secret-mstoken", "ttwid": "1|fake-secret-ttwid"}
        response = Response(400, "Bad Request", Headers({
            "Handshake-Msg": "fake handshake reason",
            "Set-Cookie": "sessionid=fake-secret-setcookie",
        }), body=b'{"msToken":"fake-secret-body"}')
        connect = AsyncMock(side_effect=InvalidStatus(response))
        send_target = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            initialize_fixture(str(Path(tmp) / "ledger.db"))
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({"session": "fake", "message": "hi",
                                        "targets": [{"conv_id": "0:1:1:2", "conv_short_id": 7, "conv_type": 1}]}),
                            encoding="utf-8")
            output = io.StringIO()
            with patch.object(oneshot, "load_session", return_value=dict(cookies)), \
                    patch.object(oneshot, "get_own_user_id", return_value="111"), \
                    patch("websockets.connect", connect), \
                    patch.object(oneshot, "_send_target_oneshot", send_target), \
                    contextlib.redirect_stdout(output):
                with self.assertRaises(__import__('circuit_breaker').CircuitOpen):
                    asyncio.run(oneshot.run_oneshot_send(config_path=str(plan), validate_server=False,
                                                         db_path=str(Path(tmp) / "ledger.db")))
        send_target.assert_not_called()
        self.assertIn("ttwid=1|fake-secret-ttwid", connect.call_args.args[0])
        text = output.getvalue()
        self.assertIn("im-ws-sg.tiktok.com/ws/v2", text)
        self.assertIn("query params: device_platform, version_code, access_key, fpid, aid, ttwid, xsack", text)
        self.assertNotIn("fake handshake reason", text)
        self.assertIn("response: 400", text)
        self.assertNotIn("fake-secret", text)

    def test_ws_probe_handshakes_then_closes_without_sending_frames(self):
        from unittest.mock import AsyncMock, MagicMock
        ws = MagicMock()
        ws.send, ws.recv, ws.close = AsyncMock(), AsyncMock(), AsyncMock()
        connect = AsyncMock(return_value=ws)
        cookies = {"sessionid": "fake-sid", "ttwid": "1|fake", "msToken": "fake"}
        with patch.object(oneshot, "load_session", return_value=cookies), \
                patch.object(oneshot, "load_ws_auth", return_value={"ttwid": "1|browser|1|bb", "access_key": "c" * 32}), \
                patch("websockets.connect", connect), \
                patch.object(ledger, "RunLock") as run_lock, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.cmd_ws_probe("fake", host="im-ws-va.tiktok.com"), 0)
        url = connect.call_args.args[0]
        self.assertTrue(url.startswith("wss://im-ws-va.tiktok.com/ws/v2?"))
        self.assertIn(f"access_key={'c' * 32}&fpid=9&aid=1459&ttwid=1|browser|1|bb&", url)
        cookie_hdr = dict(connect.call_args.kwargs["additional_headers"])["Cookie"]
        self.assertIn("ttwid=1|browser|1|bb", cookie_hdr)
        self.assertNotIn("1|fake", cookie_hdr)
        self.assertNotIn("c" * 32, output.getvalue())
        ws.close.assert_awaited_once()
        ws.send.assert_not_called()
        ws.recv.assert_not_called()
        run_lock.assert_not_called()
        self.assertIn("No message was transmitted", output.getvalue())

    def test_ws_probe_refuses_non_tiktok_ws_host_before_connecting(self):
        from unittest.mock import AsyncMock
        connect = AsyncMock()
        for host in ("evil.example.com", "im-ws-sg.tiktok.com.evil.com", "www.tiktok.com", "user@im-ws.tiktok.com"):
            with self.subTest(host=host), \
                    patch.object(oneshot, "load_session", return_value={"sessionid": "fake-sid"}), \
                    patch("websockets.connect", connect), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_ws_probe("fake", host=host), 1)
        connect.assert_not_called()

    def test_ws_probe_reports_rejection_without_secrets(self):
        from unittest.mock import AsyncMock
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response
        response = Response(400, "Bad Request", Headers({"Handshake-Msg": "authentication failed"}), body=b"")
        with patch.object(oneshot, "load_session", return_value={"sessionid": "fake-secret-sid", "ttwid": "fake-secret-tw"}), \
                patch("websockets.connect", AsyncMock(side_effect=InvalidStatus(response))), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.cmd_ws_probe("fake"), 1)
        self.assertNotIn("authentication failed", output.getvalue())
        self.assertIn("HTTP 400", output.getvalue())
        self.assertNotIn("fake-secret", output.getvalue())

    def test_qr_login_keeps_ttwid_from_login_cookie_jar(self):
        from types import SimpleNamespace as C
        jar = [C(name="ttwid", value="1|fake"), C(name="tt_chain_token", value="fake"), C(name="sessionid", value="old")]
        merged = qrlogin._merge_jar_cookies({"sessionid": "confirmed"}, jar)
        self.assertEqual(merged, {"sessionid": "confirmed", "ttwid": "1|fake"})
        self.assertEqual(qrlogin._merge_jar_cookies({"ttwid": "server"}, jar)["ttwid"], "server")

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
            reserve_at("111", "0:1:111:222", "fake-uuid", date="2026-10-08", db_path=path)
            with self.assertRaises(ledger.QuotaExceededError):
                reserve_at("111", "0:1:111:222", "different-uuid", date="2026-10-09", db_path=path)

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
            initialize_fixture(db)
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
                    patch("ledger_requests.utc_now_ms", return_value=1791435600000), \
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
