"""Command Line Interface for TikTok Streak Manager.
Phase A: Interactive CLI.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

if sys.stdout and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import main as main_cli
import oneshot
import ledger
from services import (
    list_sessions, fetch_inbox, save_plan, Conversation,
    CONFIG_PATH, PREFS_PATH, read_json_object, save_preferences
)


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    clear_screen()
    print("==========================")
    print(" TikTok Streak Manager CLI")
    print("==========================")


def get_cli_status() -> dict:
    try:
        cfg = read_json_object(CONFIG_PATH)
        account = cfg.get("session", "Unknown")
    except Exception:
        account = "Unknown"
        
    try:
        prefs = read_json_object(PREFS_PATH)
        scheduler = "Running" if False else "Stopped" # Placeholder for Phase C
    except Exception:
        scheduler = "Stopped"

    return {
        "account": account,
        "scheduler": scheduler
    }


def menu_manage_accounts():
    sessions = list_sessions()
    print("\n--- [1] Quản lý tài khoản ---")
    if not sessions:
        print("Chưa có tài khoản nào được lưu.")
        return
    
    print("Các tài khoản đã lưu:")
    for i, s in enumerate(sessions, 1):
        print(f" {i}. {s}")
    
    print("\nLưu ý: Để thêm tài khoản, chạy 'python main.py login' và 'python main.py capture-ws-auth'.")
    input("\nNhấn Enter để quay lại...")


def menu_list_conversations():
    print("\n--- [2] Danh sách hội thoại ---")
    cfg = read_json_object(CONFIG_PATH)
    session = cfg.get("session")
    if not session:
        sessions = list_sessions()
        if not sessions:
            print("Chưa có tài khoản. Vui lòng login trước.")
            input("\nNhấn Enter để quay lại...")
            return
        session = sessions[0]
        
    print(f"Đang tải hội thoại cho tài khoản: {session}...")
    try:
        convs = asyncio.run(asyncio.to_thread(fetch_inbox, session))
        print(f"\nTìm thấy {len(convs)} hội thoại:")
        print(f"{'#':<4} {'Type':<10} {'Short ID':<20} {'Name'}")
        print("-" * 60)
        for i, c in enumerate(convs, 1):
            ctype = "Group" if c.conv_type == 2 else "Direct"
            print(f"{i:<4} {ctype:<10} {c.conv_short_id:<20} {c.name}")
    except Exception as e:
        print(f"Lỗi: {e}")
    input("\nNhấn Enter để quay lại...")


def menu_select_targets():
    print("\n--- [3] Chọn người/nhóm nhận tin ---")
    sessions = list_sessions()
    if not sessions:
        print("Chưa có tài khoản.")
        input("\nNhấn Enter để quay lại...")
        return
    
    print("Chọn tài khoản:")
    for i, s in enumerate(sessions, 1):
        print(f" {i}. {s}")
    choice = input("Nhập số: ").strip()
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(sessions)):
            raise ValueError()
        session = sessions[idx]
    except Exception:
        print("Lựa chọn không hợp lệ.")
        input("\nNhấn Enter để quay lại...")
        return

    print(f"Đang tải hội thoại cho tài khoản: {session}...")
    try:
        convs = asyncio.run(asyncio.to_thread(fetch_inbox, session))
    except Exception as e:
        print(f"Lỗi: {e}")
        input("\nNhấn Enter để quay lại...")
        return

    for i, c in enumerate(convs, 1):
        print(f" {i}. {c.name} (Type: {c.conv_type}, ID: {c.conv_short_id})")
        
    choices = input("\nNhập các số thứ tự cần chọn (cách nhau bằng dấu phẩy): ").strip()
    if not choices:
        return
    
    try:
        selected_idxs = [int(x.strip()) - 1 for x in choices.split(",")]
        targets = [convs[x] for x in selected_idxs if 0 <= x < len(convs)]
        if not targets:
            print("Không có hội thoại nào được chọn.")
            input("\nNhấn Enter để quay lại...")
            return
            
        cfg = read_json_object(CONFIG_PATH)
        msg = cfg.get("message", "Hello")
        save_plan(session, msg, targets)
        print(f"Đã lưu {len(targets)} hội thoại vào cấu hình.")
    except Exception as e:
        print(f"Lỗi lưu: {e}")
        
    input("\nNhấn Enter để quay lại...")


def menu_config_message():
    print("\n--- [4] Cấu hình nội dung ---")
    cfg = read_json_object(CONFIG_PATH)
    if 'rotation' in cfg:
        print("Plan đang dùng rotation. Hãy chỉnh rotation.templates trong file cấu hình; menu này chỉ sửa tin cố định.")
        input("\nNhấn Enter để quay lại...")
        return
    old_msg = cfg.get("message", "(Chưa có)")
    print(f"Nội dung hiện tại: {old_msg}")
    new_msg = input("Nhập nội dung mới (để trống để giữ nguyên): ").strip()
    
    if new_msg:
        try:
            import services
            services.save_fixed_message(new_msg, CONFIG_PATH)
            print("Đã lưu tin nhắn.")
        except Exception as e:
            print(f"Lỗi lưu: {e}")
            
    input("\nNhấn Enter để quay lại...")


def menu_config_schedule():
    print("\n--- [5] Cấu hình lịch gửi ---")
    prefs = read_json_object(PREFS_PATH)
    settings = read_json_object(CONFIG_PATH)
    prefs["skip_if_sent"] = not settings.get("allow_repeat_same_day", False)
    print(f"Giờ gửi hiện tại: {prefs.get('time', '08:00')}")
    print(f"Tự động: {'Bật' if prefs.get('enabled') else 'Tắt'}")
    print(f"Chống gửi trùng: {'Bật' if prefs.get('skip_if_sent', True) else 'Tắt'}")
    
    new_time = input("\nNhập giờ gửi mới (HH:MM) [để trống giữ nguyên]: ").strip()
    if not new_time:
        new_time = prefs.get('time', '08:00')
        
    en = input("Bật tự động gửi? (y/n) [để trống giữ nguyên]: ").strip().lower()
    if en == 'y':
        new_enabled = True
    elif en == 'n':
        new_enabled = False
    else:
        new_enabled = prefs.get('enabled', False)
        
    skip = input("Bật chống gửi trùng? (y/n) [để trống giữ nguyên]: ").strip().lower()
    if skip == 'y':
        new_skip = True
    elif skip == 'n':
        new_skip = False
    else:
        new_skip = prefs.get('skip_if_sent', True)
        
    try:
        from services import save_send_settings
        current_limit = settings.get("max_sends_per_conversation_per_day", 1)
        entered = input(f"Hạn mức mỗi hội thoại/ngày [{current_limit}]: ").strip()
        limit = int(entered) if entered else current_limit
        from ledger_requests import DEFAULT_SETTINGS
        limits = {}
        for name in ("max_sends_per_account_per_day", "min_send_interval_seconds", "max_sends_per_window", "window_seconds"):
            current = settings.get(name, DEFAULT_SETTINGS[name])
            entered = input(f"{name} [{current}]: ").strip()
            limits[name] = int(entered) if entered else current
        save_send_settings(not new_skip, limit, **limits)
        save_preferences(new_time, new_enabled, skip_if_sent=new_skip)
        print("Đã lưu cấu hình lịch gửi.")
    except Exception as e:
        print(f"Lỗi: {e}")
        
    input("\nNhấn Enter để quay lại...")


def menu_dry_run():
    print("\n--- [6] Offline Dry-run ---")
    main_cli.cmd_dry_run(str(CONFIG_PATH))
    input("\nNhấn Enter để quay lại...")


def menu_check_connection():
    print("\n--- [7] Kiểm tra kết nối ---")
    cfg = read_json_object(CONFIG_PATH)
    session = cfg.get("session")
    if not session:
        print("Chưa cấu hình session.")
    else:
        print(f"Đang ping WebSocket (ws-probe) cho session: {session}...")
        main_cli.cmd_ws_probe(session)
    input("\nNhấn Enter để quay lại...")


def menu_send_manual():
    print("\n--- [8] Gửi thử thủ công ---")
    print("CẢNH BÁO: Hành động này sẽ gửi tin nhắn thật lên server TikTok!")
    
    while True:
        confirm = input("Bạn có chắc chắn muốn gửi? (yes/no): ").strip().lower()
        if confirm in ["yes", "no"]:
            break
        if confirm == "":
            continue # Ignore stray newlines
            
    if confirm == "yes":
        from services import summarize_results
        try:
            from services import send_plan
            import uuid
            result = asyncio.run(send_plan(CONFIG_PATH, idempotency_key="manual:" + str(uuid.uuid4()),
                                           confirm_request=main_cli.confirm_snapshot))
            confirmed, skipped, failed = summarize_results(result)
            print(f"\nKết quả: {confirmed} thành công, {skipped} bỏ qua, {failed} thất bại.")
            for row in result.get("results", []):
                print(row.get("request_id", ""), row.get("detail") or row.get("reason", ""))
        except Exception as e:
            print(f"Lỗi khi gửi: {e}")
    else:
        print("Đã hủy.")
    input("\nNhấn Enter để quay lại...")


def menu_scheduler_toggle():
    print("\n--- [9] Khởi động / Dừng Scheduler ---")
    print("Ghi chú: Tính năng này đang được phát triển (Phase C).")
    input("\nNhấn Enter để quay lại...")


def menu_status_history():
    print("\n--- [10] Xem trạng thái và lịch sử ---")
    main_cli.cmd_status(str(CONFIG_PATH))
    input("\nNhấn Enter để quay lại...")


def interactive_mode():
    while True:
        print_header()
        st = get_cli_status()
        print(f"Account: {st['account']}")
        print("Login: Chưa kiểm tra")
        print("WebSocket: Chưa kiểm tra (dùng mục 7 để probe)")
        print(f"Scheduler: {st['scheduler']}\n")
        
        print("[1] Quản lý tài khoản")
        print("[2] Danh sách hội thoại")
        print("[3] Chọn người/nhóm nhận tin")
        print("[4] Cấu hình nội dung")
        print("[5] Cấu hình lịch gửi")
        print("[6] Offline Dry-run")
        print("[7] Kiểm tra kết nối")
        print("[8] Gửi thử thủ công")
        print("[9] Khởi động / dừng Scheduler (Phase C)")
        print("[10] Xem trạng thái và lịch sử")
        print("[0] Thoát")
        
        choice = input("\nChọn chức năng: ").strip()
        if choice == "0":
            break
        elif choice == "1":
            menu_manage_accounts()
        elif choice == "2":
            menu_list_conversations()
        elif choice == "3":
            menu_select_targets()
        elif choice == "4":
            menu_config_message()
        elif choice == "5":
            menu_config_schedule()
        elif choice == "6":
            menu_dry_run()
        elif choice == "7":
            menu_check_connection()
        elif choice == "8":
            menu_send_manual()
        elif choice == "9":
            menu_scheduler_toggle()
        elif choice == "10":
            menu_status_history()
        else:
            print("Lựa chọn không hợp lệ.")
            import time
            time.sleep(1)


def parse_args():
    parser = argparse.ArgumentParser(description="TikTok Streak Manager CLI")
    subparsers = parser.add_subparsers(dest="command", help="CLI commands")
    
    subparsers.add_parser("status", help="Xem trạng thái ledger")
    subparsers.add_parser("accounts", help="Quản lý tài khoản")
    subparsers.add_parser("conversations", help="Danh sách hội thoại")
    subparsers.add_parser("configure", help="Cấu hình hệ thống (interactively)")
    subparsers.add_parser("dry-run", help="Chạy offline dry-run")
    
    sched_parser = subparsers.add_parser("scheduler", help="Scheduler daemon")
    sched_parser.add_argument("action", choices=["start", "stop", "status"], help="Hành động scheduler")

    return parser.parse_args()


def main():
    args = parse_args()
    
    if args.command is None:
        interactive_mode()
    elif args.command == "status":
        main_cli.cmd_status(str(CONFIG_PATH))
    elif args.command == "accounts":
        sessions = list_sessions()
        for s in sessions:
            print(s)
    elif args.command == "conversations":
        cfg = read_json_object(CONFIG_PATH)
        session = cfg.get("session")
        if session:
            main_cli.cmd_list_conversations(session)
        else:
            print("Chưa lưu session vào cấu hình.")
    elif args.command == "configure":
        menu_config_message()
        menu_config_schedule()
    elif args.command == "dry-run":
        main_cli.cmd_dry_run(str(CONFIG_PATH))
    elif args.command == "scheduler":
        print(f"Scheduler {args.action} is planned for Phase C.")


if __name__ == "__main__":
    main()
