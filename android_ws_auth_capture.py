"""
Termux-native WebSocket Auth Capture for TikTok via Chrome DevTools Protocol (CDP).
Eliminates the need for Windows Playwright.

Usage:
  adb forward tcp:9222 localabstract:chrome_devtools_remote
  python android_ws_auth_capture.py --session <session_name>
"""

import argparse
import asyncio
import hashlib
import json
import urllib.request
import urllib.parse
from typing import Optional

try:
    import websockets
except ImportError:
    print("[!] Thiếu thư viện websockets. Chạy: pip install websockets")
    exit(1)

# Local imports from existing backend
import qrlogin
from oneshot import probe_ws_handshake

CDP_JSON_URL = "http://127.0.0.1:9222/json/list"


def get_tiktok_tab_ws_url() -> Optional[str]:
    try:
        req = urllib.request.Request(CDP_JSON_URL)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            
        for tab in data:
            url = tab.get("url", "")
            if "tiktok.com" in url:
                ws_url = tab.get("webSocketDebuggerUrl")
                if ws_url:
                    return ws_url
        return None
    except Exception:
        return None


async def capture_cdp_auth() -> Optional[tuple[str, str]]:
    ws_url = get_tiktok_tab_ws_url()
    if not ws_url:
        print("[-] Không tìm thấy tab nào mở tiktok.com qua CDP.")
        print("    Vui lòng mở Chrome trên điện thoại, truy cập tiktok.com và thử lại.")
        return None
        
    print(f"[*] Đang kết nối Chrome CDP (ẩn URL)...")
    
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            print("[+] Kết nối CDP thành công. Vui lòng thao tác trên Chrome (VD: vào mục Tin nhắn, tải lại trang)...")
            await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                except asyncio.TimeoutError:
                    print("[-] Quá thời gian chờ (60s). Vui lòng chạy lại và tải lại trang TikTok trên điện thoại.")
                    return None
                    
                data = json.loads(msg)
                if data.get("method") == "Network.webSocketCreated":
                    url = data.get("params", {}).get("url", "")
                    if "im-ws" in url and "tiktok.com" in url:
                        parsed = urllib.parse.urlparse(url)
                        params = urllib.parse.parse_qs(parsed.query)
                        
                        access_key = params.get("access_key", [None])[0]
                        ttwid = params.get("ttwid", [None])[0]
                        device_id = params.get("device_id", [None])[0]
                        
                        if access_key and ttwid:
                            print("[+] Đã bắt được WebSocket Auth (ttwid & access_key).")
                            return (ttwid, access_key, device_id)
                        else:
                            print("[-] Bắt được WS nhưng thiếu access_key hoặc ttwid. Đang chờ tiếp...")
    except websockets.exceptions.ConnectionClosed:
        print("[!] Mất kết nối tới Chrome.")
    except Exception as e:
        print(f"[!] Lỗi khi giao tiếp CDP: {type(e).__name__}")
        
    return None


def run_capture(session_name: str):
    print("=== TERMUX TIKTOK WS AUTH CAPTURE ===")
    
    cookies = qrlogin.load_session(session_name)
    if not cookies.get("sessionid"):
        print(f"[!] Session '{session_name}' không hợp lệ hoặc chưa đăng nhập.")
        return
        
    session_hash = hashlib.sha256(cookies["sessionid"].encode()).hexdigest()
    
    # 1. Capture via CDP
    pair = asyncio.run(capture_cdp_auth())
    if not pair:
        print("[!] Không thu thập được Auth. Hủy thao tác.")
        return
        
    ttwid, access_key, device_id = pair
    auth_candidate = {"ttwid": ttwid, "access_key": access_key}
    if device_id:
        auth_candidate["device_id"] = device_id
    
    # 2. Verify with ws-probe in memory (no overwrite yet)
    print("\n[*] Đang kiểm chứng Auth thu thập được (ws-probe)...")
    try:
        asyncio.run(probe_ws_handshake(session_name, ws_auth=auth_candidate))
        print("[+] Xác minh thành công. Kết nối Handshake OK!")
    except Exception as e:
        print(f"[!] Xác minh Handshake thất bại: {type(e).__name__} - {e}")
        print("    Auth không hợp lệ, KHÔNG LƯU VÀO FILE.")
        return
        
    # 3. Save to ws_auth.local.json
    path = qrlogin.ws_auth_path(session_name)
    payload = {
        **auth_candidate,
        "session": session_name,
        "session_hash": session_hash
    }
    
    qrlogin.write_private_json(path, payload)
    print(f"\n[SUCCESS] Đã lưu Auth (Session-bound) vào: {path}")
    print("[*] Bot đã sẵn sàng gửi tin hằng ngày độc lập hoàn toàn trên Termux!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capture TikTok WS Auth natively on Android/Termux")
    parser.add_argument("--session", "-s", required=True, help="Tên session đã đăng nhập")
    args = parser.parse_args()
    
    run_capture(args.session)
