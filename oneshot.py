import asyncio
import json
import os
import re
import ssl
import sys
import time
import uuid
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import config
import ledger
import log as _log
from client import LttkClient, apply_ws_auth, build_ws_url, load_ws_auth
from core.api import get_conversations, get_own_user_id
from core.proto import build_ws_packet
from main import validate_config_data
from qrlogin import load_session


_SAFE_RESPONSE_HEADERS = (
    "handshake-msg", "handshake-status", "x-tt-logid", "server", "content-type", "content-length",
)


def describe_ws_rejection(exc, url: str, headers: list[tuple[str, str]], cookies: dict) -> list[str]:
    """Summarize a rejected WS upgrade without exposing cookie/token/session values.

    Only names are reported for query params, request headers, and cookies; response
    headers are whitelisted (Set-Cookie is never included) and values are redacted.
    """
    from urllib.parse import urlsplit, parse_qsl

    def _clip(value, limit: int = 200) -> str:
        text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
        return _log.redact(text[:limit])

    parts = urlsplit(url)
    lines = [
        f"endpoint: {parts.hostname}{parts.path}",
        "query params: " + ", ".join(k for k, _ in parse_qsl(parts.query, keep_blank_values=True)),
        "request headers: " + ", ".join(k for k, _ in headers),
        "cookie names: " + ", ".join(sorted(cookies)),
        f"ttwid present: {bool(cookies.get('ttwid'))}, msToken present: {bool(cookies.get('msToken'))}",
    ]
    response = getattr(exc, "response", None)
    if response is None:
        lines.append("response: unavailable")
        return lines
    lines.append(f"response: {getattr(response, 'status_code', '?')} {_clip(getattr(response, 'reason_phrase', ''), 60)}")
    resp_headers = getattr(response, "headers", None)
    if resp_headers is not None:
        for name in _SAFE_RESPONSE_HEADERS:
            value = resp_headers.get(name)
            if value:
                lines.append(f"response header {name}: {_clip(value)}")
    body = getattr(response, "body", None)
    if body:
        lines.append(f"response body ({len(body)} bytes): {_clip(body.decode('utf-8', errors='replace'))}")
    return lines


def validate_against_server_inbox(cookies: dict, targets: list[dict], device_id: str | None = None) -> None:
    """Validate configured conversation targets against actual server inbox metadata.

    Ensures that conversation exists and configured conv_short_id and conv_type
    match the server-reported metadata before any send attempt.
    """
    _log.info("oneshot", "Validating targets against server conversation inbox...")
    inbox_convs = get_conversations(cookies=cookies, device_id=device_id)
    server_map = {c["conv_id"]: c for c in inbox_convs}

    for t in targets:
        cid = t["conv_id"]
        if cid not in server_map:
            raise ValueError(
                f"Target conversation '{cid}' was not found in TikTok server inbox. "
                f"Ensure the account has an existing conversation with this target."
            )
        srv = server_map[cid]
        srv_short_id = srv.get("conv_short_id", 0)
        srv_type = srv.get("conv_type", 1)

        if t["conv_short_id"] != srv_short_id:
            raise ValueError(
                f"Target '{cid}' metadata mismatch: configured conv_short_id is {t['conv_short_id']}, "
                f"but server inbox reports {srv_short_id}. Aborting send."
            )
        if t["conv_type"] != srv_type:
            raise ValueError(
                f"Target '{cid}' type mismatch: configured conv_type is {t['conv_type']}, "
                f"but server inbox reports {srv_type}. Aborting send."
            )
    _log.ok("oneshot", f"All {len(targets)} targets successfully validated against server inbox.")


async def _send_target_oneshot(
    ws,
    canonical_uid: str,
    target: dict,
    message_text: str,
    timeout_seconds: float = 20.0,
    db_path: str = ledger._DEFAULT_LEDGER_PATH,
) -> dict:
    """Execute durable reservation, transmission, and correlated echo confirmation for one target."""
    conv_id = target["conv_id"]
    conv_short_id = target["conv_short_id"]
    conv_type = target["conv_type"]

    # Recompute Ho Chi Minh date immediately before reservation
    send_date = ledger.get_current_ho_chi_minh_date()
    client_msg_id = str(uuid.uuid4())

    # Check already attempted
    attempted, status = ledger.is_already_attempted(canonical_uid, conv_id, date=send_date, db_path=db_path)
    if attempted:
        _log.warn("oneshot", f"[{conv_id}] Already recorded as '{status}' for today ({send_date}), skipping.")
        return {"conv_id": conv_id, "status": "skipped", "reason": f"already_{status}"}

    # Durable reservation BEFORE sending
    try:
        ledger.reserve_pending(canonical_uid, conv_id, client_msg_id, date=send_date, db_path=db_path)
    except ledger.QuotaExceededError as e:
        _log.warn("oneshot", f"[{conv_id}] Quota already claimed: {e}")
        return {"conv_id": conv_id, "status": "skipped", "reason": "quota_claimed"}

    _log.info("oneshot", f"[{conv_id}] Reserved pending ({client_msg_id}). Transmitting packet...")

    # Build verified packet
    packet, msg_type, _ = build_ws_packet(
        conv_id=conv_id,
        short_id=conv_short_id,
        text=message_text,
        device_id=config.DEVICE_ID,
        sdk_ms_token=config.MSG_SDK_MS_TOKEN,
        tt_public_key=config.TT_PUBLIC_KEY,
        tt_client_data=config.TT_CLIENT_DATA,
        conv_type=conv_type,
        client_id=client_msg_id,
    )

    start_monotonic = time.monotonic()
    deadline = start_monotonic + timeout_seconds

    # Transmit over WebSocket with bounded timeout
    try:
        remaining_send = max(0.1, deadline - time.monotonic())
        await asyncio.wait_for(ws.send(packet), timeout=min(5.0, remaining_send))
    except Exception:
        _log.error("oneshot", f"[{conv_id}] WebSocket transmit failed ({ledger.REASON_WS_ERROR})")
        ledger.mark_failed_unknown(
            canonical_uid, conv_id, send_date, client_msg_id,
            reason_code=ledger.REASON_WS_ERROR, db_path=db_path
        )
        return {"conv_id": conv_id, "status": ledger.STATUS_FAILED_UNKNOWN, "reason": ledger.REASON_WS_ERROR}

    # Wait for correlated server echo with finite monotonic timeout
    confirmed = False
    server_msg_id = None
    failure_reason = ledger.REASON_CORRELATION_TIMEOUT

    try:
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            if not isinstance(raw, bytes):
                continue

            decompressed = LttkClient._decompress_lz4_frame(raw)
            frames_to_check = [raw]
            if decompressed:
                frames_to_check.append(decompressed)

            # Check for ban / restriction / error indications in both raw and decompressed frames
            is_banned_or_error = False
            for f in frames_to_check:
                if (b'sending_ban' in f or b'"ban"' in f or b'restricted' in f
                        or b'restriction' in f or (b'"status_code":' in f and b'"status_code":0' not in f)
                        or b'"error"' in f or b'error_code' in f):
                    is_banned_or_error = True
                    break

            if is_banned_or_error:
                _log.error("oneshot", f"[{conv_id}] Server returned restriction or error frame ({ledger.REASON_WS_ERROR})")
                failure_reason = ledger.REASON_WS_ERROR
                break

            candidates = LttkClient.find_all_msgbodies(decompressed or raw)
            for cand in candidates:
                is_match, reason, sid = LttkClient.check_echo_correlation(
                    cand,
                    expected_client_msg_id=client_msg_id,
                    expected_conv_id=conv_id,
                    own_user_id=canonical_uid,
                    expected_text=message_text,
                )
                if is_match and sid:
                    confirmed = True
                    server_msg_id = str(sid)
                    break

            if confirmed:
                break

    except Exception:
        _log.error("oneshot", f"[{conv_id}] Exception while awaiting server echo ({ledger.REASON_CLIENT_EXCEPTION})")
        failure_reason = ledger.REASON_CLIENT_EXCEPTION

    if confirmed and server_msg_id:
        confirm_date = ledger.get_current_ho_chi_minh_date()
        ledger.confirm_send(
            canonical_uid, conv_id, send_date, client_msg_id,
            server_msg_id, confirm_date=confirm_date, db_path=db_path
        )
        _log.ok("oneshot", f"[{conv_id}] Confirmed server echo received: server_msg_id={server_msg_id}")
        return {"conv_id": conv_id, "status": ledger.STATUS_CONFIRMED, "server_msg_id": server_msg_id}
    else:
        _log.warn("oneshot", f"[{conv_id}] Send unconfirmed; recorded as failed_unknown ({failure_reason}). No retries today.")
        ledger.mark_failed_unknown(
            canonical_uid, conv_id, send_date, client_msg_id,
            reason_code=failure_reason, db_path=db_path
        )
        return {"conv_id": conv_id, "status": ledger.STATUS_FAILED_UNKNOWN, "reason": failure_reason}


_RE_TIKTOK_WS_HOST = re.compile(r"^im-ws(?:-[a-z0-9]+)?\.tiktok\.com$")


def with_ws_host(url: str, host: str) -> str:
    """Swap the WS host; only im-ws*.tiktok.com is allowed so cookies never go elsewhere."""
    host = (host or "").strip().lower()
    if not _RE_TIKTOK_WS_HOST.match(host):
        raise ValueError(f"Refusing WebSocket host {host!r}: must look like im-ws-<region>.tiktok.com")
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=host))


def _prepare_ws(cookies: dict) -> tuple[dict, str]:
    """Apply the local browser ttwid/access_key pair (if any) and build the handshake URL."""
    ws_auth = load_ws_auth()
    if ws_auth:
        _log.info("oneshot", f"WS auth: using ttwid/access_key pair from {config.WS_AUTH_FILE}")
    else:
        _log.warn("oneshot", f"WS auth: {config.WS_AUTH_FILE} not found; using built-in access_key, "
                             "which only matches the upstream ttwid and will likely be rejected")
    ws_cookies = apply_ws_auth(cookies, ws_auth)
    return ws_cookies, build_ws_url(ws_cookies, access_key=ws_auth.get("access_key"))


async def _open_ws(cookies: dict, ws_url: str):
    """Perform the verified-TLS WS handshake; log safe diagnostics if the server rejects it."""
    import websockets
    from websockets.exceptions import InvalidStatus

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = True
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED

    cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
        ("Origin", "https://www.tiktok.com"),
        ("Cookie", cookie_hdr),
        ("Pragma", "no-cache"),
        ("Cache-Control", "no-cache"),
    ]
    ws_kw = "additional_headers" if tuple(int(x) for x in websockets.__version__.split(".")[:2]) >= (14, 0) else "extra_headers"

    if not cookies.get("ttwid"):
        _log.warn("oneshot", "Session has no ttwid cookie; the WebSocket gateway may reject the upgrade. "
                             "Re-run 'python main.py login' to refresh the session.")

    _log.info("oneshot", f"Connecting to TikTok WebSocket ({urlsplit(ws_url).hostname}) with verified TLS...")
    try:
        return await websockets.connect(
            ws_url,
            **{ws_kw: headers},
            subprotocols=["binary", "base64", "pbbp2"],
            ssl=ssl_ctx,
            ping_interval=20,
            ping_timeout=10,
        )
    except InvalidStatus as e:
        for line in describe_ws_rejection(e, ws_url, headers, cookies):
            _log.error("oneshot", f"WS handshake rejected - {line}")
        raise


async def probe_ws_handshake(session_name: str, host: str | None = None) -> None:
    """Handshake-only diagnostic: connect, then close immediately.

    Sends no frames at all (not even the "hi" ping), touches no ledger and reads no
    streak config, so it can never transmit a message.
    """
    cookies = load_session(session_name)
    if not cookies or not cookies.get("sessionid"):
        raise ValueError(f"Session '{session_name}' not found or missing sessionid cookie.")
    ws_cookies, ws_url = _prepare_ws(cookies)
    if host:
        ws_url = with_ws_host(ws_url, host)
    connection = await _open_ws(ws_cookies, ws_url)
    await connection.close()
    _log.ok("oneshot", f"Handshake OK on {urlsplit(ws_url).hostname}; connection closed without sending any frame.")


async def run_oneshot_send(
    config_path: str = "streak.json",
    timeout_seconds: float = 20.0,
    validate_server: bool = True,
    db_path: str = ledger._DEFAULT_LEDGER_PATH,
) -> dict:
    """Execute one-shot streak transmission workflow."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if not isinstance(cfg, dict):
        raise ValueError("Configuration file must contain a JSON object")

    valid, err, targets = validate_config_data(cfg)
    if not valid:
        raise ValueError(f"Configuration validation failed: {err}")

    session_name = cfg["session"]
    message_text = cfg["message"]

    cookies = load_session(session_name)
    if not cookies or not cookies.get("sessionid"):
        raise ValueError(
            f"Session '{session_name}' not found or missing sessionid cookie. "
            f"Please run 'python main.py login' first."
        )

    canonical_uid = get_own_user_id(cookies=cookies)
    if not canonical_uid:
        raise RuntimeError("Failed to resolve canonical own user ID from server. Session may be expired.")

    _log.info("oneshot", f"Authenticated canonical UID: {canonical_uid}")

    if validate_server:
        validate_against_server_inbox(cookies, targets, device_id=config.DEVICE_ID)

    # Acquire process run lock
    with ledger.RunLock():
        results = []
        connection = await _open_ws(*_prepare_ws(cookies))
        async with connection as ws:
            _log.ok("oneshot", "Connected to WebSocket. Processing targets sequentially...")
            # Handshake ping
            await asyncio.wait_for(ws.send("hi"), timeout=5.0)
            try:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            for target in targets:
                res = await _send_target_oneshot(
                    ws=ws,
                    canonical_uid=canonical_uid,
                    target=target,
                    message_text=message_text,
                    timeout_seconds=timeout_seconds,
                    db_path=db_path,
                )
                results.append(res)

        return {
            "canonical_uid": canonical_uid,
            "date": ledger.get_current_ho_chi_minh_date(),
            "results": results,
        }
