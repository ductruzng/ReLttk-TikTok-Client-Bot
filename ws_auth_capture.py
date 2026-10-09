"""Optional Windows browser auth setup. Captures in memory, probes without a DM,
and writes session-bound credentials only after a successful handshake.
"""

import asyncio
import hashlib
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit

TARGET_URL = "https://www.tiktok.com/messages"
TARGET_HOST = "im-ws-sg.tiktok.com"
CAPTURE_MS = 30_000

ACCESS_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
TTWID_RE = re.compile(r"^1\|[^|]+\|[^|]+\|[^|]+$")


def pick_one(params, name):
    values = params.get(name, [])
    return values[0] if len(values) == 1 else None


def valid_pair(ttwid, access_key):
    return bool(
        isinstance(ttwid, str)
        and isinstance(access_key, str)
        and TTWID_RE.fullmatch(ttwid)
        and ACCESS_KEY_RE.fullmatch(access_key)
    )


def capture_for_cookies(cookies_dict: dict = None, headless: bool = True):
    if sys.platform != "win32":
        raise RuntimeError("Browser auth capture is supported by this project on Windows only")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 850})

        if cookies_dict:
            playwright_cookies = []
            for k, v in cookies_dict.items():
                playwright_cookies.append({
                    "name": k,
                    "value": str(v),
                    "domain": ".tiktok.com",
                    "path": "/"
                })
            context.add_cookies(playwright_cookies)
            
        records = {}
        attached_pages = set()

        def attach(page):
            page_id = id(page)
            if page_id in attached_pages:
                return
            attached_pages.add(page_id)
            try:
                cdp = context.new_cdp_session(page)
            except Exception:
                return

            def created(event):
                parts = urlsplit(event.get("url", ""))
                if parts.hostname != TARGET_HOST or parts.path != "/ws/v2":
                    return
                params = parse_qs(parts.query, keep_blank_values=True)
                ttwid = pick_one(params, "ttwid")
                key = pick_one(params, "access_key")
                pair = (unquote(ttwid), key.lower()) if ttwid and key else None
                records[(page_id, event["requestId"])] = {
                    "pair": pair if pair and valid_pair(*pair) else None,
                    "status101": False, "sent": 0, "received": 0, "error": False,
                }
                print("[WS] Detected (values hidden)")

            def record_for(event):
                return records.get((page_id, event.get("requestId")))

            def handshake(event):
                record = record_for(event)
                if record is not None:
                    record["status101"] = event.get("response", {}).get("status") == 101

            def frame_sent(event):
                record = record_for(event)
                if record is not None:
                    record["sent"] += 1

            def frame_received(event):
                record = record_for(event)
                if record is not None:
                    record["received"] += 1

            def frame_error(event):
                record = record_for(event)
                if record is not None:
                    record["error"] = True

            cdp.on("Network.webSocketCreated", created)
            cdp.on("Network.webSocketHandshakeResponseReceived", handshake)
            cdp.on("Network.webSocketFrameSent", frame_sent)
            cdp.on("Network.webSocketFrameReceived", frame_received)
            cdp.on("Network.webSocketFrameError", frame_error)
            cdp.send("Network.enable")

        try:
            context.on("page", attach)
            for tab in context.pages:
                attach(tab)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
            print("[+] Monitoring for WebSockets (max 30 seconds)...")
            
            # Instead of waiting a fixed 30s, we wait in chunks and return early if we found a good connection
            elapsed = 0
            found_pair = None
            while elapsed < CAPTURE_MS:
                page.wait_for_timeout(1000)
                elapsed += 1000
                eligible = [
                    rec for rec in records.values()
                    if rec["pair"] and rec["status101"]
                    and rec["sent"] > 0 and rec["received"] > 0
                    and not rec["error"]
                ]
                if eligible:
                    distinct = {rec["pair"] for rec in eligible}
                    if len(distinct) == 1:
                        found_pair = next(iter(distinct))
                        break

            if found_pair:
                return found_pair
            return None
        finally:
            context.close()
            browser.close()


def capture_session_auth(session_name: str, cookies: dict | None = None) -> None:
    from qrlogin import load_session, write_private_json, ws_auth_path
    if sys.platform != "win32":
        raise RuntimeError("Capture auth on Windows, then follow TERMUX.md")
    path = ws_auth_path(session_name)
    if cookies is None:
        cookies = load_session(session_name)
    if not cookies.get("sessionid"):
        raise ValueError("A saved authenticated session is required")
    pair = capture_for_cookies(cookies)
    if not pair:
        raise RuntimeError("No qualified WebSocket auth candidate; nothing saved")
    auth = {"ttwid": pair[0], "access_key": pair[1]}
    from oneshot import probe_ws_handshake
    # Probe the candidate in memory; never temporarily replace global credentials.
    asyncio.run(probe_ws_handshake(session_name, ws_auth=auth))
    payload = {**auth, "session": session_name,
               "session_hash": hashlib.sha256(cookies["sessionid"].encode()).hexdigest()}
    write_private_json(path, payload)
    print("[+] Session-bound WebSocket auth saved. No direct message was sent.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    try:
        capture_session_auth(args.session)
    except Exception as exc:
        print(f"[!] Browser auth setup failed: {type(exc).__name__}")
        raise SystemExit(1)
