"""Safe, testable service layer for TikTok Streak Manager TUI v0.2.

Network access occurs only through explicit fetch_inbox() or send_plan() calls.
Importing this module performs no network or runtime-data access.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "streak.local.json"
PREFS_PATH = ROOT / "work" / "tui-settings.local.json"
SESSION_DIR = ROOT / "sesion"  # spelling used by the upstream project
SESSION_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Conversation:
    conv_id: str
    conv_short_id: int
    conv_type: int
    name: str

    def target(self) -> dict[str, Any]:
        return {
            "conv_id": self.conv_id,
            "conv_short_id": self.conv_short_id,
            "conv_type": self.conv_type,
        }


def valid_session_name(name: str) -> bool:
    return bool(
        isinstance(name, str)
        and name
        and name not in (".", "..")
        and ".." not in name
        and SESSION_PATTERN.fullmatch(name)
    )


def list_sessions(directory: Path = SESSION_DIR) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(
        (p.stem for p in directory.glob("*.json")
         if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".json"
         and valid_session_name(p.stem)),
        key=str.casefold,
    )


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Do not corrupt the existing JSON on an interrupted write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tui-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def parse_inbox(raw: object) -> list[Conversation]:
    """Normalize ONLY IDs confirmed by the server response; never synthesize IDs."""
    if not isinstance(raw, list):
        raise ValueError("Unexpected inbox response type")
    output: list[Conversation] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        conv_id = item.get("conv_id")
        short = item.get("conv_short_id")
        typ = item.get("conv_type")
        if not isinstance(conv_id, str) or not conv_id.strip():
            continue
        conv_id = conv_id.strip()
        if isinstance(short, bool) or isinstance(typ, bool):
            continue
        try:
            if isinstance(short, float) or isinstance(typ, float):
                continue
            short_id = int(short)
            conv_type = int(typ)
        except (ValueError, TypeError):
            continue
        if not 0 < short_id <= 0xFFFFFFFFFFFFFFFF or conv_type not in (1, 2):
            continue
        if conv_id in seen:
            continue
        seen.add(conv_id)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            name = "(ChÃƒâ€ Ã‚Â°a cÃƒÆ’Ã‚Â³ tÃƒÆ’Ã‚Âªn)"
        name = " ".join(name.split())[:100]
        output.append(Conversation(conv_id, short_id, conv_type, name))
    return output


def fetch_inbox(session_name: str) -> list[Conversation]:
    """Live read-only TikTok inbox call. Invoke from a background worker only."""
    if not valid_session_name(session_name) or session_name not in list_sessions():
        raise ValueError("Choose an existing saved session")

    import qrlogin
    import config
    import core.api as api

    cookies = qrlogin.load_session(session_name)
    if not isinstance(cookies, dict) or not cookies.get("sessionid"):
        raise ValueError("Session is missing sessionid; log in again")
    response = api.get_conversations(cookies=cookies, device_id=config.DEVICE_ID)
    return parse_inbox(response)


def matching_selected_ids(
    previous_targets: object,
    conversations: list[Conversation],
) -> set[str]:
    """Restore selection only if ID + short ID + type all match."""
    if not isinstance(previous_targets, list):
        return set()
    old = set()
    for item in previous_targets:
        if not isinstance(item, dict):
            continue
        try:
            old.add((item["conv_id"], int(item["conv_short_id"]), int(item["conv_type"])))
        except (KeyError, ValueError, TypeError):
            continue
    return {
        c.conv_id for c in conversations
        if (c.conv_id, c.conv_short_id, c.conv_type) in old
    }


def save_plan(
    session: str,
    message: str,
    recipients: list[Conversation],
    path: Path = CONFIG_PATH,
    *, allow_repeat_same_day: bool | None = None,
) -> dict[str, Any]:
    if not valid_session_name(session):
        raise ValueError("Invalid session name")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Message must not be empty")
    if not recipients:
        raise ValueError("Select at least one conversation")
    if len({c.conv_id for c in recipients}) != len(recipients):
        raise ValueError("Duplicate conversation detected")
    current = read_json_object(path)
    if 'rotation' in current:
        raise ValueError('Rotation plan is managed through CLI/config; TUI cannot overwrite templates')
    current.update({
        "session": session,
        "message": message.strip(),
        "targets": [c.target() for c in recipients],
    })
    if allow_repeat_same_day is not None:
        current["allow_repeat_same_day"] = allow_repeat_same_day
    from ledger_requests import DEFAULT_SETTINGS
    for name, default in DEFAULT_SETTINGS.items():
        current.setdefault(name, default)
    from main import validate_config
    valid, errors, _ = validate_config(current)
    if not valid:
        raise ValueError(f"Config validation failed ({len(errors)} issue(s))")
    atomic_write_json(path, current)
    return current


def save_preferences(
    send_time: str,
    enabled: bool,
    path: Path = PREFS_PATH,
    skip_if_sent: bool = True,
) -> dict[str, Any]:
    if not TIME_PATTERN.fullmatch(send_time):
        raise ValueError("Time must be HH:MM (24-hour format)")
    data = {
        "time": send_time,
        "enabled": bool(enabled),
        "skip_if_sent": bool(skip_if_sent),
        "timezone": "Asia/Ho_Chi_Minh",
    }
    atomic_write_json(path, data)
    return data


def scheduled_run_key(prefs: dict, now, last_run: str) -> str | None:
    """One in-memory trigger per scheduled day; the durable ledger guards restarts."""
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    if prefs.get("enabled") is not True or local.strftime("%H:%M") != prefs.get("time"):
        return None
    key = local.strftime("%Y-%m-%d %H:%M")
    return key if key != last_run else None


def summarize_results(result: dict) -> tuple[int, int, int]:
    rows = result.get("results", [])
    if not rows:
        raise ValueError("No send results returned")
    confirmed = sum(r.get("status") == "confirmed" for r in rows)
    skipped = sum(r.get("status") == "skipped" for r in rows)
    return confirmed, skipped, len(rows) - confirmed - skipped


def send_result_completed(row: dict) -> bool:
    """A skipped operation is complete only when its durable request is confirmed."""
    return row.get('status') == 'confirmed' or (
        row.get('status') == 'skipped' and row.get('existing_status') == 'confirmed')


def save_fixed_message(message: str, path=CONFIG_PATH):
    import rotation
    from ledger_requests import settings_from_config
    cfg = read_json_object(Path(path))
    if 'rotation' in cfg:
        raise ValueError('Plan uses rotation; edit rotation.templates in the configuration instead')
    cfg['message'] = message
    rotation.catalog(cfg)
    settings_from_config(cfg)
    atomic_write_json(Path(path), cfg)
    return cfg


async def send_plan(config_path=CONFIG_PATH, *, idempotency_key=None, **options):
    """Shared CLI/TUI send entry. Repeat policy comes from the plan, never deletion."""
    from oneshot import run_oneshot_send
    return await run_oneshot_send(config_path=str(config_path), idempotency_key=idempotency_key, **options)


def request_preview(request_id, *, db_path, config_path=None):
    import ledger
    import ledger_requests as requests
    ledger.check_ready(db_path)
    row = requests.get_request(request_id, db_path=db_path)
    preview = {'request': row, 'payload': json.loads(row['payload_json'])}
    if row['campaign_id'] is not None and config_path:
        import rotation
        cfg = read_json_object(Path(config_path))
        preview['review'] = requests.snapshot_review(row, cfg)
        preview['current_catalog'] = rotation.catalog(cfg)
        preview['configuration_changed'] = (preview['review']['config_fingerprint'] !=
                                            json.loads(row['snapshot_json'])['fingerprint'])
    return preview


def cancel_request(request_id, reason, *, db_path):
    from ledger_requests import cancel_request as cancel
    return cancel(request_id, reason, db_path=db_path)


def approve_request_snapshot(request_id, preview, reason, *, db_path, config_path):
    from ledger_requests import approve_snapshot
    return approve_snapshot(request_id, read_json_object(Path(config_path)), preview['review'], reason, db_path=db_path)


def rotation_skip_preview(scope, *, db_path):
    from ledger_requests import preview_skip
    return preview_skip(scope, db_path=db_path)


def rotation_skip(scope, preview, reason, *, db_path):
    from ledger_requests import skip_selection
    return skip_selection(scope, preview, reason, db_path=db_path)


def save_send_settings(allow_repeat_same_day, max_sends_per_conversation_per_day, path=CONFIG_PATH, **limits):
    from ledger_requests import settings_from_config, DEFAULT_SETTINGS
    if set(limits) - DEFAULT_SETTINGS.keys():
        raise ValueError("Unknown rate-limit setting")
    cfg = read_json_object(path)
    cfg.update(allow_repeat_same_day=allow_repeat_same_day,
               max_sends_per_conversation_per_day=max_sends_per_conversation_per_day, **limits)
    cfg.update(settings_from_config(cfg))
    atomic_write_json(path, cfg)
    return cfg


def breaker_status(*, db_path, account=None, session=None, history=False):
    import circuit_breaker
    return circuit_breaker.inspect(db_path=db_path, uid=account, session=session, history=history)


def breaker_reset(scope_type, scope_key, preview, reason, *, db_path):
    import circuit_breaker
    return circuit_breaker.reset(scope_type, scope_key, preview, reason, db_path=db_path)
