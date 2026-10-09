"""Offline regressions for quota, scheduler, auth isolation and safe diagnostics."""
import asyncio
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import client
import ledger
import main
import oneshot
import qrlogin
import tui_services as services
import ws_auth_capture as capture


class SafetyFixTests(unittest.TestCase):
    def test_schedule_two_days_and_local_midnight(self):
        prefs = {"enabled": True, "time": "08:00"}
        first = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
        second = first.replace(day=10)
        key = services.scheduled_run_key(prefs, first, "")
        self.assertEqual(key, "2026-10-09 08:00")
        self.assertIsNone(services.scheduled_run_key(prefs, first, key))
        self.assertEqual(services.scheduled_run_key(prefs, second, key), "2026-10-10 08:00")
        self.assertIsNone(services.scheduled_run_key({}, first, ""))
        self.assertIsNone(services.scheduled_run_key({**prefs, "enabled": False}, first, ""))
        self.assertIsNone(services.scheduled_run_key(prefs, first.replace(hour=2), ""))

    def test_results_distinguish_skip_from_confirmation(self):
        self.assertEqual(services.summarize_results({"results": [
            {"status": "confirmed"}, {"status": "skipped", "reason": "already_confirmed"},
            {"status": "failed_unknown"},
        ]}), (1, 1, 1))
        with self.assertRaises(ValueError):
            services.summarize_results({"results": []})

    def test_force_cannot_erase_pending_or_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ledger.db")
            ledger.reserve_pending("111", "conv", "old", date="2026-10-08", db_path=db)
            with self.assertRaises(TypeError):
                ledger.reserve_pending("111", "conv", "new", date="2026-10-09", db_path=db, force=True)
            with self.assertRaises(ledger.QuotaExceededError):
                ledger.reserve_pending("111", "conv", "new", date="2026-10-09", db_path=db)
            self.assertEqual(ledger.is_already_attempted("111", "conv", "2026-10-08", db), (True, "pending"))

    def test_tui_rejects_invalid_target_before_replacing_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text('{"note":"keep"}', encoding="utf-8")
            for bad in (services.Conversation("conv", 0, 1, "name"),
                        services.Conversation("conv", 10, 99, "name")):
                with self.assertRaises(ValueError):
                    services.save_plan("fake", "hi", [bad], path)
                self.assertEqual(json.loads(path.read_text()), {"note": "keep"})

    def test_cli_exception_never_prints_raw_text_or_traceback(self):
        output = io.StringIO()
        with patch.object(oneshot, "run_oneshot_send", AsyncMock(side_effect=RuntimeError("raw-secret-value"))), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(main.cmd_send("unused.json"), 1)
        self.assertNotIn("raw-secret-value", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())

    def test_auth_capture_only_saves_after_probe_and_is_bound_to_session(self):
        cookies = {"sessionid": "fake-only-sid"}
        pair = ("1|fake|123|abc", "a" * 32)
        with tempfile.TemporaryDirectory() as tmp, patch.object(qrlogin, "_SESSION_DIR", tmp), \
                patch.object(capture.sys, "platform", "win32"), \
                patch.object(capture, "capture_for_cookies", return_value=pair), \
                contextlib.redirect_stdout(io.StringIO()):
            target = Path(qrlogin.ws_auth_path("fake"))
            with patch.object(oneshot, "probe_ws_handshake", AsyncMock(side_effect=RuntimeError("failed"))):
                with self.assertRaises(RuntimeError):
                    capture.capture_session_auth("fake", cookies)
                self.assertFalse(target.exists())
            with patch.object(oneshot, "probe_ws_handshake", AsyncMock()) as probe:
                capture.capture_session_auth("fake", cookies)
                probe.assert_awaited_once_with("fake", ws_auth={"ttwid": pair[0], "access_key": pair[1]})
            self.assertEqual(client.load_ws_auth(session_name="fake", cookies=cookies),
                             {"ttwid": pair[0], "access_key": pair[1]})
            with self.assertRaises(ValueError):
                client.load_ws_auth(session_name="fake", cookies={"sessionid": "changed-session"})
            self.assertEqual(client.load_ws_auth(session_name="another", cookies=cookies), {})
            self.assertNotIn("fake-only-sid", target.read_text())

    def test_missing_auth_fails_before_connect(self):
        with patch.object(oneshot, "load_ws_auth", return_value={}), \
                patch.object(oneshot, "load_session", return_value={"sessionid": "fake"}), \
                patch.object(oneshot, "_open_ws", AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                asyncio.run(oneshot.probe_ws_handshake("fake"))
            connect.assert_not_called()

    def test_private_write_fails_closed_without_replacing_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text("old", encoding="utf-8")
            with patch.object(qrlogin.sys, "platform", "linux"), \
                    patch.object(qrlogin.os, "chmod", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    qrlogin.write_private_json(str(path), {"access_key": "fake"})
            self.assertEqual(path.read_text(), "old")
            with patch.object(qrlogin.os.path, "islink", side_effect=lambda p: str(p) == str(path)):
                with self.assertRaises(PermissionError):
                    qrlogin.write_private_json(str(path), {})
            self.assertEqual(path.read_text(), "old")

    def test_auth_read_fails_before_open_if_chmod_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(client.sys, "platform", "linux"), \
                    patch.object(client.os, "chmod", side_effect=PermissionError("denied")), \
                    patch.object(client.os, "open") as opened:
                with self.assertRaises(PermissionError):
                    client.load_ws_auth(str(path))
                opened.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional Textual not installed")
class TUISmoke(unittest.IsolatedAsyncioTestCase):
    async def test_mount_is_offline_and_schedule_defaults_off(self):
        from tiktok_tui import TikTokTUI
        from textual.widgets import Switch
        with patch("tiktok_tui.list_sessions", return_value=[]), \
                patch("tiktok_tui.read_json_object", return_value={}), \
                patch.object(TikTokTUI, "_run_send") as send:
            app = TikTokTUI()
            async with app.run_test():
                self.assertFalse(app.query_one("#enable-schedule", Switch).value)
                app._check_schedule()
                send.assert_not_called()
