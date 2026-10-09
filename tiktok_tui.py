"""TikTok Streak Manager - Modern Dashboard TUI v0.3.

Run: python -B tiktok_tui.py
Or:  python -B main.py tui
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import work
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, RichLog,
    Select, Static, Switch,
)

from tui_services import (
    CONFIG_PATH, PREFS_PATH, ROOT, Conversation,
    fetch_inbox, list_sessions, matching_selected_ids,
    read_json_object, save_plan, save_preferences,
)


class TikTokTUI(App):
    TITLE = "TikTok Streak Manager"
    SUB_TITLE = "Modern All-in-One Dashboard"
    
    CSS = """
    Screen {
        background: #0b0f19;
        color: #e2e8f0;
    }

    Header {
        background: #111827;
        color: #38bdf8;
        dock: top;
        height: 1;
    }

    Footer {
        background: #111827;
        color: #94a3b8;
        dock: bottom;
        height: 1;
    }

    #main-container {
        height: 1fr;
        layout: horizontal;
        padding: 0 1;
    }

    /* LEFT SIDEBAR: Controls */
    #sidebar {
        width: 48;
        height: 1fr;
        background: #161f30;
        border: round #334155;
        padding: 1;
        margin-right: 1;
    }

    #sidebar:focus-within {
        border: round #38bdf8;
    }

    .panel-title {
        color: #38bdf8;
        text-style: bold;
        margin-bottom: 0;
    }

    .section-label {
        color: #94a3b8;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }

    /* Account row */
    #account-row {
        height: 3;
        margin-bottom: 0;
    }

    #account {
        width: 1fr;
        margin-bottom: 0;
    }

    .icon-btn {
        min-width: 6;
        height: 3;
        margin-left: 1;
        padding: 0 1;
    }

    /* Inputs */
    Input {
        background: #0b0f19;
        border: tall #334155;
        color: #f8fafc;
        margin-bottom: 0;
    }

    Input:focus {
        border: tall #38bdf8;
    }

    Select {
        background: #0b0f19;
        border: tall #334155;
        margin-bottom: 0;
    }

    Select:focus {
        border: tall #38bdf8;
    }

    /* Schedule inputs */
    #time-row {
        height: 3;
        align-vertical: middle;
        margin-top: 0;
        margin-bottom: 0;
    }

    #time-label {
        width: 1fr;
        color: #cbd5e1;
        padding-top: 1;
    }

    #send-time {
        width: 12;
        margin-bottom: 0;
    }

    .switch-row {
        height: auto;
        align-vertical: middle;
        margin-top: 1;
    }

    .switch-text {
        width: 1fr;
        color: #cbd5e1;
    }

    Switch {
        margin-left: 1;
    }

    /* Action Buttons */
    #save-all {
        width: 100%;
        height: 3;
        margin-top: 1;
        margin-bottom: 0;
    }

    .action-row {
        height: 3;
        margin-top: 1;
        margin-bottom: 0;
    }

    .half-btn {
        width: 1fr;
        height: 3;
    }

    #send-now {
        margin-right: 1;
    }

    #info {
        color: #94a3b8;
        margin-top: 1;
        height: auto;
    }

    /* RIGHT CONTENT PANE */
    #content-pane {
        width: 1fr;
        height: 1fr;
        layout: vertical;
    }

    #inbox-card {
        height: 58%;
        background: #161f30;
        border: round #334155;
        padding: 1;
        margin-bottom: 1;
    }

    #inbox-card:focus-within {
        border: round #38bdf8;
    }

    #log-card {
        height: 42%;
        background: #161f30;
        border: round #334155;
        padding: 1;
    }

    #log-card:focus-within {
        border: round #38bdf8;
    }

    #search {
        width: 100%;
        margin-bottom: 1;
    }

    #table-toolbar {
        height: 3;
        margin-bottom: 1;
        align-vertical: middle;
    }

    #table-toolbar Button {
        height: 3;
        margin-right: 1;
        min-width: 12;
    }

    #inbox-counter {
        color: #38bdf8;
        padding-top: 1;
        margin-left: 1;
        width: 1fr;
    }

    #table {
        height: 1fr;
        background: #0b0f19;
        border: tall #1e293b;
    }

    #activity {
        height: 1fr;
        background: #0b0f19;
        border: tall #1e293b;
        color: #94a3b8;
    }
    """

    BINDINGS = [
        ("ctrl+r", "refresh", "Làm mới tài khoản"),
        ("ctrl+s", "sync", "Đồng bộ Inbox"),
        ("ctrl+q", "quit", "Thoát"),
    ]

    def __init__(self):
        super().__init__()
        self.conversations: list[Conversation] = []
        self.selected_ids: set[str] = set()
        self.loaded_for: str | None = None
        self._initialized = False
        self._fetching = False
        self._last_run_time = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            # LEFT SIDEBAR
            with VerticalScroll(id="sidebar"):
                yield Static("⚙️ BẢNG ĐIỀU KHIỂN & CẤU HÌNH", classes="panel-title")
                
                # Section 1: Account
                yield Static("👤 Tài khoản TikTok:", classes="section-label")
                with Horizontal(id="account-row"):
                    yield Select([], prompt="-- Chọn tài khoản --", allow_blank=True, id="account")
                    yield Button("🔄", id="refresh", classes="icon-btn")
                    yield Button("📷 QR", id="login", classes="icon-btn")

                # Section 2: Message Content
                yield Static("📝 Tin nhắn duy trì streak:", classes="section-label")
                yield Input(placeholder="Nhập nội dung tin nhắn gửi...", id="message")

                # Section 3: Schedule & Policies
                yield Static("⏰ Cài đặt lịch gửi hàng ngày:", classes="section-label")
                with Horizontal(id="time-row"):
                    yield Static("Giờ gửi (HH:MM):", id="time-label")
                    yield Input(value="08:00", id="send-time")
                
                with Horizontal(classes="switch-row"):
                    yield Static("Bật Scheduler tự động:", classes="switch-text")
                    yield Switch(value=False, id="enable-schedule")

                with Horizontal(classes="switch-row"):
                    yield Static("Bỏ qua nếu đã gửi hôm nay:", classes="switch-text")
                    yield Switch(value=True, id="skip-sent")

                # Section 4: Action Buttons
                yield Button("💾 LƯU CẤU HÌNH & LỊCH", id="save-all", variant="primary")
                with Horizontal(classes="action-row"):
                    yield Button("⚡ GỬI NGAY", id="send-now", variant="success", classes="half-btn")
                    yield Button("🧪 DRY-RUN", id="dryrun", variant="default", classes="half-btn")

                yield Static("Trạng thái: Chưa chọn tài khoản", id="info")

            # RIGHT CONTENT
            with Vertical(id="content-pane"):
                # Inbox Card
                with Vertical(id="inbox-card"):
                    yield Static("👥 DANH SÁCH HỘI THOẠI (BẠN BÈ & NHÓM)", classes="panel-title")
                    yield Input(placeholder="🔍 Tìm bạn bè, nhóm hoặc ID hội thoại...", id="search")
                    with Horizontal(id="table-toolbar"):
                        yield Button("🔄 Đồng bộ Inbox", id="sync", variant="primary")
                        yield Button("✓ Chọn tất cả", id="select-visible")
                        yield Button("✕ Bỏ chọn", id="clear-selection")
                        yield Static("Chưa đồng bộ hộp thoại", id="inbox-counter")
                    yield DataTable(id="table", cursor_type="row", zebra_stripes=True)

                # Activity Log Card
                with Vertical(id="log-card"):
                    yield Static("📜 NHẬT KÝ HOẠT ĐỘNG (LIVE LOGS)", classes="panel-title")
                    yield RichLog(id="activity", markup=False, wrap=True, auto_scroll=True)

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("✓", "Tên hiển thị (Nickname)", "Phân loại", "Short ID", "Conversation ID")
        self.refresh_sessions(initial=True)
        self._initialized = True
        self._activity("🚀 TikTok Streak Manager v0.3 Dashboard đã sẵn sàng.")
        self.set_interval(60.0, self._check_schedule)

    def _check_schedule(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tui_services import scheduled_run_key
        try:
            prefs = read_json_object(PREFS_PATH)
            key = scheduled_run_key(
                prefs,
                datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")),
                self._last_run_time,
            )
        except (OSError, ValueError):
            return
        if key:
            self._last_run_time = key
            self._activity(f"[Scheduler] ⏰ Đã đến giờ gửi tin tự động ({prefs.get('time')}). Khởi chạy tiến trình...")
            self._run_send("Scheduler")

    def _activity(self, text: str) -> None:
        self.query_one("#activity", RichLog).write(text)

    def _session(self) -> str:
        selected = self.query_one("#account", Select).value
        return selected if isinstance(selected, str) else ""

    def _saved_plan(self) -> dict:
        return read_json_object(CONFIG_PATH)

    def refresh_sessions(self, initial: bool = False) -> None:
        try:
            names = list_sessions()
            options = [(name, name) for name in names]
            account = self.query_one("#account", Select)
            previous = self._session()
            saved = self._saved_plan()
            preferred = previous if previous in names else saved.get("session", "")
            if preferred not in names:
                preferred = ""
            account.set_options(options)
            account.value = preferred if preferred else Select.NULL
            if initial:
                self.query_one("#message", Input).value = str(saved.get("message", ""))
                prefs = read_json_object(PREFS_PATH)
                self.query_one("#send-time", Input).value = str(prefs.get("time", "08:00"))
                self.query_one("#enable-schedule", Switch).value = bool(prefs.get("enabled", False))
                self.query_one("#skip-sent", Switch).value = bool(prefs.get("skip_if_sent", True))
            self._update_summary()
            if not initial:
                self.notify(f"Tìm thấy {len(names)} tài khoản đã lưu")
        except (OSError, ValueError) as exc:
            self.notify(f"Không thể đọc cấu hình: {type(exc).__name__}", severity="error")

    def _update_summary(self) -> None:
        try:
            config = self._saved_plan()
            prefs = read_json_object(PREFS_PATH)
        except (OSError, ValueError):
            config = {}
            prefs = {}
        
        status = "BẬT" if prefs.get("enabled") else "TẮT"
        account_name = self._session() or "(Chưa chọn)"
        targets_count = len(config.get('targets', [])) if isinstance(config.get('targets'), list) else 0
        
        self.query_one("#info", Static).update(
            f"Tài khoản: {account_name}\nĐã lưu: {targets_count} người nhận | Scheduler: {status}"
        )

        visible_count = len(self._visible()) if self.loaded_for == self._session() else 0
        total_count = len(self.conversations) if self.loaded_for == self._session() else 0
        selected_count = len(self.selected_ids)
        
        if self.loaded_for != self._session():
            self.query_one("#inbox-counter", Static).update("Chưa đồng bộ tài khoản này")
        else:
            self.query_one("#inbox-counter", Static).update(
                f"Đã chọn: {selected_count} | Hiển thị: {visible_count}/{total_count}"
            )

    def _reset_inbox(self) -> None:
        self.loaded_for = None
        self.conversations = []
        self.selected_ids.clear()
        self.query_one("#table", DataTable).clear()
        self._update_summary()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._initialized and event.select.id == "account":
            self._reset_inbox()
            self._activity(f"🔄 Đã chuyển sang tài khoản: {self._session() or '(Không chọn)'}. Vui lòng bấm 'Đồng bộ Inbox'.")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._render_table()

    def _visible(self) -> list[Conversation]:
        keyword = self.query_one("#search", Input).value.strip().casefold()
        if not keyword:
            return self.conversations
        return [c for c in self.conversations if keyword in c.name.casefold()
                or keyword in c.conv_id.casefold() or keyword in str(c.conv_short_id)]

    def _render_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        if self.loaded_for != self._session():
            return
        visible = self._visible()
        for c in visible:
            is_selected = c.conv_id in self.selected_ids
            check_box = "✓ [X]" if is_selected else "  [ ]"
            conv_kind = "👥 Nhóm" if c.conv_type == 2 else "💬 Cá nhân"
            table.add_row(
                check_box,
                c.name,
                conv_kind,
                str(c.conv_short_id),
                c.conv_id,
                key=c.conv_id,
            )
        self._update_summary()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "table" or self.loaded_for != self._session():
            return
        conv_id = str(event.row_key.value)
        if conv_id in self.selected_ids:
            self.selected_ids.remove(conv_id)
        else:
            self.selected_ids.add(conv_id)
        self._render_table()

    async def _sync_inbox(self) -> None:
        session = self._session()
        if not session:
            self.notify("Vui lòng chọn một tài khoản TikTok trước khi đồng bộ!", severity="warning")
            return
        if self._fetching:
            return
        self._fetching = True
        self.query_one("#sync", Button).disabled = True
        self._activity(f"⏳ Đang kết nối máy chủ TikTok đồng bộ hội thoại cho '{session}'...")
        try:
            conversations = await asyncio.to_thread(fetch_inbox, session)
            if session != self._session():
                self._activity("Tài khoản đã đổi trong lúc tải; đã hủy kết quả cũ.")
                return
            config = self._saved_plan()
            old = config.get("targets", []) if config.get("session") == session else []
            self.conversations = conversations
            self.loaded_for = session
            self.selected_ids = matching_selected_ids(old, conversations)
            self._render_table()
            self._activity(f"✅ Đồng bộ thành công: Tìm thấy {len(conversations)} hội thoại (Đã tích chọn lại {len(self.selected_ids)} trước đó).")
            self.notify(f"Đã đồng bộ {len(conversations)} hội thoại!", severity="information")
        except Exception as exc:
            self._activity(f"❌ Không thể đồng bộ inbox: {type(exc).__name__}.")
            self.notify("Đồng bộ thất bại; kiểm tra kết nối mạng hoặc đăng nhập lại!", severity="error")
        finally:
            self._fetching = False
            self.query_one("#sync", Button).disabled = False

    def _save_all(self) -> None:
        session = self._session()
        if not session or self.loaded_for != session:
            self.notify("Vui lòng đồng bộ Inbox của tài khoản trước khi lưu!", severity="warning")
            return
        
        chosen = [c for c in self.conversations if c.conv_id in self.selected_ids]
        if not chosen:
            self.notify("Vui lòng tích chọn ít nhất 1 người nhận!", severity="warning")
            return

        message = self.query_one("#message", Input).value.strip()
        if not message:
            self.notify("Vui lòng nhập nội dung tin nhắn!", severity="warning")
            return

        send_time = self.query_one("#send-time", Input).value.strip()
        enabled = self.query_one("#enable-schedule", Switch).value
        skip_if_sent = self.query_one("#skip-sent", Switch).value

        try:
            save_plan(session, message, chosen)
            save_preferences(send_time, enabled, skip_if_sent=skip_if_sent)
            status = "BẬT" if enabled else "TẮT"
            self._activity(f"💾 ĐÃ LƯU: {len(chosen)} người nhận | Giờ gửi: {send_time} | Scheduler: {status}")
            self.notify(f"Đã lưu thành công {len(chosen)} người nhận và cài đặt lịch!", severity="information")
            self._update_summary()
        except (ValueError, OSError) as exc:
            self.notify(f"Lỗi khi lưu cấu hình: {exc}", severity="error")
            self._activity(f"❌ Lỗi lưu cấu hình: {exc}")

    def _launch_terminal(self, *args: str) -> None:
        if os.name != "nt":
            self.notify("Chỉ mở terminal riêng tự động trên Windows", severity="warning")
            return
        try:
            subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "main.py"), *args],
                cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self._activity("📷 Đã mở cửa sổ QR Login. Hãy quét mã bằng ứng dụng TikTok trên điện thoại.")
            self.notify("Đã mở cửa sổ QR Login!", severity="information")
        except OSError:
            self.notify("Không mở được terminal đăng nhập", severity="error")

    async def _dryrun(self) -> None:
        if not CONFIG_PATH.exists():
            self.notify("Chưa có cấu hình. Hãy chọn người nhận và bấm Lưu trước.", severity="warning")
            return
        self._activity("🧪 Đang kiểm tra chạy thử (Offline Dry-run)...")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(ROOT / "main.py"),
                "--config", str(CONFIG_PATH), "--dry-run",
                cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
            content = out.decode("utf-8", errors="replace")
            for line in content.splitlines():
                if any(marker in line for marker in (
                    "Planned Targets (", "DRY-RUN COMPLETE", "Zero network calls",
                    "Config Validation Issues", "Dry-run stopped",
                )):
                    self._activity(line.strip())
            self.notify("Dry-run hoàn tất!", severity="information" if proc.returncode == 0 else "warning")
        except (OSError, asyncio.TimeoutError):
            self._activity("❌ Dry-run không hoàn thành.")
            self.notify("Không chạy được dry-run", severity="error")

    def action_refresh(self) -> None:
        self.refresh_sessions()

    def action_sync(self) -> None:
        asyncio.create_task(self._sync_inbox())

    @work(thread=True, group="send")
    def _run_send(self, source: str) -> None:
        import oneshot
        from tui_services import summarize_results
        try:
            prefs = read_json_object(PREFS_PATH)
            skip_if_sent = prefs.get("skip_if_sent", True)

            # If user wants to force-resend today, clean today's ledger records for these targets
            if not skip_if_sent and CONFIG_PATH.exists():
                cfg = read_json_object(CONFIG_PATH)
                targets = cfg.get("targets", [])
                session_name = cfg.get("session")
                if session_name and targets:
                    from qrlogin import load_session
                    from core.api import get_own_user_id
                    cookies = load_session(session_name)
                    canonical_uid = get_own_user_id(cookies)
                    if canonical_uid:
                        import ledger
                        conn = ledger._get_readwrite_connection()
                        cur = conn.cursor()
                        today = ledger.get_current_ho_chi_minh_date()
                        for t in targets:
                            cur.execute(
                                "DELETE FROM daily_ledger WHERE canonical_uid = ? AND conv_id = ? AND date = ?",
                                (str(canonical_uid), str(t.get("conv_id")), str(today))
                            )
                        conn.commit()
                        conn.close()

            result = asyncio.run(oneshot.run_oneshot_send(config_path=str(CONFIG_PATH)))
            confirmed, skipped, failed = summarize_results(result)
            text = (f"[{source}] ✅ Kết quả: {confirmed} xác nhận thành công | {skipped} bỏ qua | {failed} thất bại.")
            self.call_from_thread(self._activity, text)
            self.call_from_thread(self.notify, text, severity="warning" if failed else "information")
            for r in result.get("results", []):
                cid = r.get('conv_id', '')
                st = r.get('status', '')
                rs = r.get('reason', '')
                self.call_from_thread(self._activity, f"   ↳ {cid}: {st} {rs}")
        except Exception as exc:
            text = f"[{source}] ❌ Quá trình gửi gặp lỗi: {type(exc).__name__}."
            self.call_from_thread(self._activity, text)
            self.call_from_thread(self.notify, text, severity="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "refresh":
            self.refresh_sessions()
        elif action == "login":
            self._launch_terminal("login")
        elif action == "sync":
            await self._sync_inbox()
        elif action == "dryrun":
            await self._dryrun()
        elif action == "select-visible":
            if self.loaded_for == self._session():
                self.selected_ids.update(c.conv_id for c in self._visible())
                self._render_table()
        elif action == "clear-selection":
            self.selected_ids.clear()
            self._render_table()
        elif action == "save-all":
            self._save_all()
        elif action == "send-now":
            self._activity("⚡ [Thủ công] Bạn đã bấm 'Gửi ngay bây giờ'. Đang khởi tạo kết nối WebSocket gửi tin...")
            self.notify("Bắt đầu tiến trình gửi tin...", severity="information")
            self._run_send("Thủ công")


if __name__ == "__main__":
    TikTokTUI().run()
