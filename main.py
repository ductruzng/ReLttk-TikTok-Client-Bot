"""CLI entrypoint for ReLttk Daily Streak.

Default behavior is a one-shot offline DRY-RUN using only Python standard library.
Network transmission requires explicit --send opt-in.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Tuple, Optional

# Lazy imports for optional modules
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

DEFAULT_CONFIG_FILE = "streak.json"


def get_ho_chi_minh_date() -> str:
    """Return current date formatted for Asia/Ho_Chi_Minh timezone using stdlib."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")
    except Exception:
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=7))
    return datetime.now(tz).strftime("%Y-%m-%d")


def validate_config(cfg: object) -> Tuple[bool, list[str], list[dict]]:
    """Validate streak configuration using stdlib only.

    Returns (is_valid, error_messages, parsed_targets).
    """
    errors = []
    parsed_targets = []

    if not isinstance(cfg, dict):
        return False, ["Configuration file must contain a JSON object."], []

    session = cfg.get("session")
    if session is None or not isinstance(session, str):
        errors.append("Field 'session' must be a string.")
    elif not session.strip():
        errors.append("Field 'session' is empty. Specify the session name configured via 'python main.py login'.")
    else:
        session_str = session.strip()
        if "/" in session_str or "\\" in session_str or ".." in session_str or "\0" in session_str or not re.match(r'^[a-zA-Z0-9_\.-]+$', session_str):
            errors.append(f"Field 'session' has unsafe name {session_str!r}. Path traversal characters are disallowed.")

    import rotation
    try:
        rotation.catalog(cfg)
    except (ValueError, UnicodeError) as exc:
        errors.append(str(exc))

    from ledger_requests import settings_from_config
    try:
        settings_from_config(cfg)
    except ValueError as exc:
        errors.append(str(exc))

    raw_targets = cfg.get("targets")
    if raw_targets is None or not isinstance(raw_targets, list):
        errors.append("Field 'targets' must be a list of target objects.")
    elif len(raw_targets) == 0:
        errors.append("Field 'targets' is empty. At least one conversation target is required.")
    else:
        seen_ids = set()
        for i, t in enumerate(raw_targets):
            prefix = f"targets[{i}]"
            if not isinstance(t, dict):
                errors.append(f"{prefix} must be a JSON object with conv_id, conv_short_id, conv_type.")
                continue

            conv_id = t.get("conv_id")
            if not conv_id or not isinstance(conv_id, str) or not conv_id.strip():
                errors.append(f"{prefix}.conv_id must be a non-empty conversation identifier string.")
            else:
                conv_id = conv_id.strip()
                if conv_id in seen_ids:
                    errors.append(f"Duplicate conversation ID detected: '{conv_id}'. Each target must be unique.")
                seen_ids.add(conv_id)

            raw_short_id = t.get("conv_short_id")
            if isinstance(raw_short_id, bool) or isinstance(raw_short_id, float):
                errors.append(
                    f"{prefix}.conv_short_id must be a positive integer (> 0). Got {raw_short_id!r}."
                )
                short_id = 0
            else:
                try:
                    short_id = int(raw_short_id)
                    if short_id <= 0:
                        errors.append(
                            f"{prefix}.conv_short_id must be a positive integer (> 0). Got {raw_short_id!r}."
                        )
                        short_id = 0
                    elif short_id > 0xFFFFFFFFFFFFFFFF:
                        errors.append(
                            f"{prefix}.conv_short_id overflows 64-bit integer range. Got {raw_short_id!r}."
                        )
                        short_id = 0
                except (ValueError, TypeError):
                    errors.append(
                        f"{prefix}.conv_short_id must be a positive integer (> 0). "
                        f"Got {raw_short_id!r}. Use 'python main.py list-conversations' to obtain short IDs."
                    )
                    short_id = 0

            raw_type = t.get("conv_type", 1)
            if isinstance(raw_type, bool) or isinstance(raw_type, float):
                errors.append(f"{prefix}.conv_type must be 1 (Direct) or 2 (Group). Got {raw_type!r}.")
                conv_type = 1
            else:
                try:
                    conv_type = int(raw_type)
                    if conv_type not in (1, 2):
                        raise ValueError()
                except (ValueError, TypeError):
                    errors.append(f"{prefix}.conv_type must be 1 (Direct) or 2 (Group). Got {raw_type!r}.")
                    conv_type = 1

            parsed_targets.append({
                "conv_id": conv_id or f"target_{i}",
                "conv_short_id": short_id,
                "conv_type": conv_type,
            })

    return len(errors) == 0, errors, parsed_targets


def validate_config_data(cfg: object) -> Tuple[bool, str, list[dict]]:
    """Single strict configuration validator shared across main and oneshot."""
    valid, errors, targets = validate_config(cfg)
    return valid, "; ".join(errors), targets


def cmd_dry_run(config_path: str = DEFAULT_CONFIG_FILE) -> int:
    """Execute stdlib-only dry run display."""
    today = get_ho_chi_minh_date()

    print("=" * 64)
    print(" ReLttk Daily Streak Bot - Plan (OFFLINE DRY-RUN)")
    print("=" * 64)
    print(f" Target Date:      {today} (Asia/Ho_Chi_Minh)")
    print(f" Config File:      {os.path.abspath(config_path)}")

    if not os.path.exists(config_path):
        print(f"\n[!] Config file '{config_path}' does not exist.")
        print("    Create it with the following structure:")
        print(json.dumps({
            "session": "my_account",
            "message": "Daily streak message",
            "targets": [
                {"conv_id": "0:1:12345:67890", "conv_short_id": 1234567890, "conv_type": 1}
            ]
        }, indent=2))
        return 1

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        print(f"\n[!] Error parsing JSON in '{config_path}'.")
        return 1

    if not isinstance(cfg, dict):
        print(f"\n[!] Configuration file '{config_path}' must contain a JSON object.")
        return 1

    is_valid, errors, targets = validate_config(cfg)

    session_name = cfg.get("session") or "<none>"
    message_text = cfg.get("message") or "<none>"

    print(f" Session Name:     {session_name}")
    print(f" Message Text:     {message_text!r}")
    print("-" * 64)

    if not is_valid:
        print("[!] Configuration Validation Issues:")
        for err in errors:
            print(f"    - {err}")
        print("\n[NOTE] Dry-run stopped due to configuration errors.")
        print("       Edit streak.json to correct these fields before sending.")
        return 1

    if "rotation" in cfg:
        print(json.dumps(cfg['rotation'], ensure_ascii=True, indent=2))
        print("Rotation preview: selection requires ledger state; no choice or state change made.")
    print(f" Planned Targets ({len(targets)} total):")
    for idx, t in enumerate(targets, 1):
        type_label = "Direct (1)" if t["conv_type"] == 1 else "Group (2)"
        print(f"   {idx}. Conv ID:       {t['conv_id']}")
        print(f"      Short ID:      {t['conv_short_id']}")
        print(f"      Type:          {type_label}")
        print(f"      Ledger Status: Quota not checked offline (requires authenticated session)")

    print("=" * 64)
    print(" [DRY-RUN COMPLETE]")
    print(" - Zero network calls were made.")
    print(" - No session credentials or cookies were accessed.")
    print(" - No SQLite database files were created or modified.")
    print(" - Quota not checked offline (evaluated during live authenticated send).")
    print(" - To transmit messages for real, use explicit opt-in:")
    print(f"       python main.py --send --config {config_path}")
    print("=" * 64)
    return 0


def cmd_send(config_path: str = DEFAULT_CONFIG_FILE, idempotency_key: str | None = None, db_path: str | None = None,
             *, request_id=None, confirm_request=None) -> int:
    """Execute one-shot live transmission with explicit opt-in."""
    print("=" * 64)
    print(" ReLttk Daily Streak Bot - Executing One-Shot Send")
    print("=" * 64)

    try:
        import ledger
        import asyncio
        from services import send_plan, send_result_completed
        options = {"db_path": db_path} if db_path is not None else {}
        if request_id:
            options['request_id'] = request_id
        if confirm_request:
            options['confirm_request'] = confirm_request
        result = asyncio.run(send_plan(config_path, idempotency_key=idempotency_key, **options))
        print("\nTransmission Summary:")
        print(f" Canonical UID: {result.get('canonical_uid')}")
        print(f" Date:          {result.get('date')}")
        results_list = result.get("results", [])
        for r in results_list:
            st = r.get("status")
            cid = r.get("conv_id")
            if st == "confirmed":
                print(f"  [+] {cid}: CONFIRMED (Server Msg ID: {r.get('server_msg_id')})")
            elif st == "skipped":
                stored = r.get('existing_status', 'unspecified')
                print(f"  [-] {cid}: SKIPPED; request={stored.upper()} ({r.get('detail') or r.get('reason')})")
            else:
                label = st.upper() if isinstance(st, str) else 'INVALID_RESULT'
                print(f"  [x] {cid}: {label} ({r.get('detail') or r.get('reason')})")
        if not results_list or not all(send_result_completed(r) for r in results_list):
            return 1
        return 0
    except Exception as e:
        if not _report_ws_rejection(e):
            print(f"\n[!] Send execution failed: {type(e).__name__}")
            if isinstance(e, ledger.LedgerError):
                print(str(e))
        return 1


def _report_ws_rejection(e: Exception) -> bool:
    """Print a status-only summary for a rejected WS upgrade; return False if e is something else."""
    if type(e).__name__ != "InvalidStatus":
        return False
    status = getattr(getattr(e, "response", None), "status_code", None)
    label = f"HTTP {status}" if type(status) is int and 100 <= status <= 599 else "HTTP status unavailable"
    print(f"\n[!] WebSocket upgrade rejected ({label}). No message was transmitted.")
    print("    See the 'WS handshake rejected' diagnostics above. If ttwid is missing, re-run 'python main.py login'.")
    return True


def cmd_ws_probe(session_name: str, host: Optional[str] = None) -> int:
    """Handshake-only WebSocket check; never sends any frame or message."""
    session_name = (session_name or "").strip()
    if not session_name or not re.match(r'^[a-zA-Z0-9_\.-]+$', session_name) or ".." in session_name:
        print("[!] Invalid session name.")
        return 1
    try:
        import asyncio
        import oneshot
        asyncio.run(oneshot.probe_ws_handshake(session_name, host=host))
        print("\n[+] WebSocket handshake succeeded. No message was transmitted.")
        return 0
    except Exception as e:
        if not _report_ws_rejection(e):
            print(f"\n[!] WebSocket probe failed: {type(e).__name__}")
        return 1


def cmd_login() -> int:
    """Interactive QR code login for future user use."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("[!] Interactive QR login requires both stdin and stdout to be interactive TTY terminals.")
        return 1

    print("Starting interactive QR login...")
    try:
        import qrlogin
        qrlogin.run()
        return 0
    except Exception as e:
        print(f"[!] Login error: {type(e).__name__}")
        return 1


def cmd_capture_ws_auth(session_name: str) -> int:
    """Explicit Windows browser setup; a handshake probe never sends a DM."""
    if sys.platform != "win32":
        print("[!] Capture browser auth on Windows; see TERMUX.md for transfer instructions.")
        return 1
    try:
        from ws_auth_capture import capture_session_auth
        capture_session_auth(session_name)
        return 0
    except Exception as exc:
        print(f"[!] Browser auth setup failed: {type(exc).__name__}.")
        print("    Check optional requirements-auth-windows.txt and Chromium installation, then retry manually.")
        return 1


def cmd_list_conversations(session_name: str) -> int:
    """Fetch and display server conversation metadata for config preparation."""
    if not session_name or not session_name.strip():
        print("[!] --session is required for list-conversations.")
        return 1
    session_name = session_name.strip()
    if "/" in session_name or "\\" in session_name or ".." in session_name or "\0" in session_name:
        print("[!] Invalid session name: path traversal characters are disallowed.")
        return 1

    try:
        import qrlogin
        import core.api as api
        import config

        cookies = qrlogin.load_session(session_name)
        if not cookies.get("sessionid"):
            print(f"[!] Session '{session_name}' contains no valid sessionid.")
            return 1

        print("Fetching conversations from TikTok server inbox...")
        convs = api.get_conversations(cookies=cookies, device_id=config.DEVICE_ID)
        if not convs:
            print("No conversations found in server inbox.")
            return 0

        print(f"\nFound {len(convs)} conversations:")
        print(f"{'#':<3} {'Conv ID':<36} {'Short ID':<22} {'Type':<8} {'Name'}")
        print("-" * 85)
        for i, c in enumerate(convs, 1):
            type_str = "Group" if c.get("is_group") else "Direct"
            name_str = c.get('name', '')
            try:
                name_str.encode(sys.stdout.encoding or 'utf-8')
            except UnicodeEncodeError:
                name_str = name_str.encode('ascii', 'replace').decode('ascii')

            print(
                f"{i:<3} {c.get('conv_id', ''):<36} "
                f"{str(c.get('conv_short_id', 0)):<22} "
                f"{type_str:<8} "
                f"{name_str}"
            )
        print("\nUse the Conv ID and Short ID above in your streak.json config targets.")
        return 0
    except Exception as e:
        print(f"[!] Error fetching conversations: {type(e).__name__}")
        return 1


def cmd_status(config_path: str = DEFAULT_CONFIG_FILE, db_path: str | None = None) -> int:
    """Readonly inspection of the SQLite ledger."""
    today = get_ho_chi_minh_date()
    print(f"Ledger Status for {today} (Asia/Ho_Chi_Minh):")
    try:
        import ledger
        db_path = db_path or ledger._DEFAULT_LEDGER_PATH
        if not os.path.exists(db_path):
            print("  Ledger database file does not exist yet (clean state).")
            return 0

        conn = ledger._get_readonly_connection(db_path)
        if conn is None:
            print("  Ledger database file not accessible.")
            return 0
        try:
            cur = conn.cursor()
            if conn.execute("PRAGMA user_version").fetchone()[0] in (2, 3, 4, 5):
                for row in conn.execute("SELECT request_id,date,status,conv_id,transmitted FROM send_requests ORDER BY created_at DESC LIMIT 20"):
                    print(f"  Request {row[0]} | {row[1] or '-'} | {row[2]} | {row[3]} | transmitted={row[4]}")
                return 0
            cur.execute(
                "SELECT canonical_uid, conv_id, date, status, attempt_time, server_msg_id, reason_code "
                "FROM daily_ledger ORDER BY date DESC, attempt_time DESC LIMIT 20"
            )
            rows = cur.fetchall()
            if not rows:
                print("  No records found in daily ledger.")
                return 0
            print(f"{'Date':<12} {'Status':<16} {'Conv ID':<32} {'Server Msg ID':<16} {'Reason'}")
            print("-" * 90)
            for r in rows:
                print(f"{r[2]:<12} {r[3]:<16} {r[1]:<32} {str(r[5] or ''):<16} {str(r[6] or '')}")
            return 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Error reading ledger: {type(e).__name__}")
        return 1


def cmd_ledger_admin(command, db_path):
    import ledger_migrations
    try:
        if command == "ledger-initialize":
            ledger_migrations.initialize(db_path)
            print("Initialized schema V5. No TikTok access.")
            return 0
        report = ledger_migrations.preflight(db_path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report['errors']:
            return 1
        if command == "ledger-migrate":
            backup = ledger_migrations.migrate(db_path)
            print(f"Schema V5 ready. Backup: {backup or 'unchanged (already V5)'}")
        return 0
    except Exception as exc:
        print(f"Ledger administration failed: {type(exc).__name__}: {exc}")
        return 1


def confirm_snapshot(row):
    print('Request:', row['request_id'])
    print('Exact payload:', row['payload_json'])
    if row['snapshot_json']:
        print('Rotation snapshot:', row['snapshot_json'])
    return input('Type SEND to transmit this request: ') == 'SEND'


def cmd_request_admin(args):
    import services
    import ledger
    try:
        if args.command == 'rotation-skip-selection':
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise ValueError('Selection skip requires an interactive terminal; --yes is insufficient')
            scope = (args.account, args.conversation, args.campaign)
            preview = services.rotation_skip_preview(scope, db_path=args.db)
        else:
            preview = services.request_preview(args.request_id, db_path=args.db,
                config_path=args.config if args.command == 'request-approve-snapshot' else None)
        print(json.dumps(preview, ensure_ascii=True, indent=2))
        if args.command == 'request-show':
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError('This administration command requires interactive preview and confirmation')
        reason = input('Required reason: ')
        if input(f'Type {args.command} to confirm: ') != args.command:
            print('No changes made.')
            return 1
        if args.command == 'rotation-skip-selection':
            services.rotation_skip(scope, preview, reason, db_path=args.db)
        elif args.command == 'request-cancel':
            services.cancel_request(args.request_id, reason, db_path=args.db)
        else:
            services.approve_request_snapshot(args.request_id, preview, reason, db_path=args.db, config_path=args.config)
        print('Recorded. No message sent.')
        return 0
    except (ValueError, RuntimeError, ledger.LedgerError) as exc:
        print(f'Administration blocked: {exc}')
        return 1


def cmd_breaker_admin(args):
    import ledger
    import services
    try:
        rows = services.breaker_status(db_path=args.db, account=args.account, session=args.session,
                                       history=args.command == 'breaker-history')
        print(json.dumps(rows, ensure_ascii=True, indent=2))
        if args.command != 'breaker-reset':
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError('Reset requires interactive preview and separate confirmation')
        if len(rows) != 1:
            raise ValueError('Select exactly one existing circuit to reset')
        reason = input('Required reason (do not enter credentials): ')
        if input('Type RESET to confirm: ') != 'RESET':
            return 1
        row = rows[0]
        services.breaker_reset(row['scope_type'], row['scope_key'], row, reason, db_path=args.db)
        print('Reset recorded. Other scope barriers remain. No message sent.')
        return 0
    except (ValueError, ledger.LedgerError) as exc:
        print(f'Breaker administration blocked: {exc}')
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ReLttk Daily Streak Bot - Safe One-Shot CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG_FILE,
        help="Path to JSON configuration file (default: streak.json)",
    )

    parser.add_argument("--db", help="Existing ledger path for send/status; never created by sending")
    parser.add_argument("--idempotency-key", help="Stable task key; reuse for the same task, new key for intentional repeat")
    parser.add_argument('--yes', action='store_true', help='Noninteractive send consent; never approves changed snapshots or selection skip')
    parser.add_argument('--request-id', help='Resume this existing request without selecting a new template')
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--send",
        action="store_true",
        help="Explicit opt-in to execute live message transmission (default is offline dry-run)",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run mode (default)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Additional commands")
    for command in ('request-show', 'request-cancel', 'request-approve-snapshot', 'rotation-skip-selection'):
        admin = subparsers.add_parser(command, help='Explicit request/rotation administration; no TikTok access')
        admin.add_argument('--db', required=True)
        if command == 'rotation-skip-selection':
            for field in ('account', 'conversation', 'campaign'):
                admin.add_argument('--' + field, required=True)
        else:
            admin.add_argument('--request-id', required=True)
        if command == 'request-approve-snapshot':
            admin.add_argument('--config', required=True)

    for command in ('breaker-status', 'breaker-history', 'breaker-reset'):
        admin = subparsers.add_parser(command, help='Offline circuit administration')
        admin.add_argument('--db', required=True)
        scope = admin.add_mutually_exclusive_group(required=True)
        scope.add_argument('--account')
        scope.add_argument('--session')

    for command in ("ledger-preflight", "ledger-initialize", "ledger-migrate"):
        admin = subparsers.add_parser(command, help="Explicit offline ledger administration")
        admin.add_argument("--db", required=True, help="Exact database path (no implicit default)")

    subparsers.add_parser("login", help="Interactive QR login to save a session")

    list_parser = subparsers.add_parser(
        "list-conversations",
        help="List conversations with Conv ID and Short ID from TikTok server",
    )
    list_parser.add_argument(
        "--session", "-s",
        required=True,
        help="Session name to use (required)",
    )

    subparsers.add_parser("status", help="Inspect local SQLite ledger records in readonly mode")

    probe_parser = subparsers.add_parser(
        "ws-probe",
        help="Handshake-only WebSocket check (connects and closes; never sends a message)",
    )
    probe_parser.add_argument("--session", "-s", required=True, help="Session name to use (required)")
    probe_parser.add_argument("--host", help="Override WS host, e.g. im-ws-sg.tiktok.com (im-ws*.tiktok.com only)")

    subparsers.add_parser("tui", help="Launch the Textual Terminal User Interface")
    capture_parser = subparsers.add_parser("capture-ws-auth", help="Windows-only browser auth setup for a saved session")
    capture_parser.add_argument("--session", "-s", required=True)

    args = parser.parse_args()

    # Reject mode flags with subcommands before any network activity
    if args.command is not None and (args.send or args.dry_run):
        parser.error("Mode flags (--send, --dry-run) cannot be combined with subcommands.")

    if args.command in ('breaker-status', 'breaker-history', 'breaker-reset'):
        return cmd_breaker_admin(args)
    if args.command in ("ledger-preflight", "ledger-initialize", "ledger-migrate"):
        return cmd_ledger_admin(args.command, args.db)
    if args.command in ('request-show', 'request-cancel', 'request-approve-snapshot', 'rotation-skip-selection'):
        return cmd_request_admin(args)
    if args.command == "login":
        return cmd_login()
    elif args.command == "list-conversations":
        return cmd_list_conversations(session_name=args.session)
    elif args.command == "status":
        return cmd_status(config_path=args.config, db_path=args.db)
    elif args.command == "ws-probe":
        return cmd_ws_probe(session_name=args.session, host=args.host)
    elif args.command == "tui":
        try:
            from tiktok_tui import TikTokTUI
        except ModuleNotFoundError:
            print("[!] Optional TUI dependencies missing: install requirements-tui.txt.")
            return 1
        TikTokTUI().run()
        return 0
    elif args.command == "capture-ws-auth":
        return cmd_capture_ws_auth(args.session)

    if args.send:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not interactive and (not args.yes or not args.idempotency_key):
            parser.error('Noninteractive send requires --yes and an explicit --idempotency-key')
        return cmd_send(config_path=args.config, idempotency_key=args.idempotency_key,
                        request_id=args.request_id, confirm_request=confirm_snapshot if interactive else None,
                        **({"db_path": args.db} if args.db else {}))
    else:
        # Default behavior: offline dry-run
        return cmd_dry_run(config_path=args.config)


if __name__ == "__main__":
    sys.exit(main())
