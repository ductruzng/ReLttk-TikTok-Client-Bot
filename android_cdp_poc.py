"""
Proof of Concept: Capture TikTok WebSocket parameters via Chrome DevTools Protocol (CDP) over ADB.
Run this script inside Termux after setting up ADB port forwarding:
adb forward tcp:9222 localabstract:chrome_devtools_remote

Dependencies: pip install websockets requests
"""

import asyncio
import json
import urllib.request
import urllib.parse
from typing import Optional

try:
    import websockets
except ImportError:
    print("[!] Thiếu thư viện websockets. Chạy: pip install websockets")
    exit(1)


CDP_JSON_URL = "http://127.0.0.1:9222/json/list"


def get_tiktok_tab_ws_url() -> Optional[str]:
    """Tìm tab Chrome đang mở TikTok và trả về WebSocket Debugger URL."""
    try:
        req = urllib.request.Request(CDP_JSON_URL)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            
        for tab in data:
            url = tab.get("url", "")
            if "tiktok.com" in url:
                ws_url = tab.get("webSocketDebuggerUrl")
                if ws_url:
                    print(f"[+] Tìm thấy tab TikTok: {tab.get('title', 'Unknown')} ({url})")
                    return ws_url
                    
        print("[-] Không tìm thấy tab nào mở tiktok.com")
        return None
    except Exception as e:
        print(f"[!] Lỗi kết nối đến Chrome CDP ({CDP_JSON_URL}): {e}")
        print("    Bạn đã chạy 'adb forward tcp:9222 localabstract:chrome_devtools_remote' chưa?")
        return None


async def monitor_cdp(ws_url: str):
    """Kết nối CDP và lắng nghe sự kiện WebSocket được tạo."""
    print(f"[*] Đang kết nối CDP: {ws_url}")
    
    # Required keys we want to verify
    required_keys = {"access_key", "f_wt", "device_id", "app_name"}
    
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            print("[+] Kết nối CDP thành công. Kích hoạt Network tracking...")
            
            # Enable Network domain
            await ws.send(json.dumps({
                "id": 1,
                "method": "Network.enable"
            }))
            
            print("[*] Đang chờ sự kiện tạo WebSocket từ TikTok... (Vui lòng tải lại trang TikTok trên Chrome hoặc vào mục Tin nhắn)")
            
            while True:
                msg = await ws.recv()
                data = json.loads(msg)
                
                # Check for Network.webSocketCreated or request events
                method = data.get("method", "")
                
                if method == "Network.webSocketCreated":
                    url = data.get("params", {}).get("url", "")
                    if "im-ws" in url and "tiktok.com" in url:
                        print("\n[+] ĐÃ PHÁT HIỆN WEB_SOCKET CỦA TIKTOK!")
                        
                        parsed = urllib.parse.urlparse(url)
                        params = urllib.parse.parse_qs(parsed.query)
                        
                        found_keys = set(params.keys())
                        missing = required_keys - found_keys
                        
                        print("\n--- KẾT QUẢ KIỂM TRA THAM SỐ (REDACTED) ---")
                        for k, v in params.items():
                            # Redact value, just show length or presence
                            val_list = v[0] if isinstance(v, list) and v else str(v)
                            redacted_val = f"*** (length: {len(val_list)})" if val_list else "EMPTY"
                            mark = "✅" if k in required_keys else "  "
                            print(f"{mark} {k}: {redacted_val}")
                            
                        if not missing:
                            print("\n[SUCCESS] Tất cả các tham số xác thực cần thiết đều CÓ MẶT.")
                            print("[!] PoC Hoàn thành. An toàn thoát.")
                            break
                        else:
                            print(f"\n[WARNING] Thiếu các tham số: {missing}")
                            print("[*] Tiếp tục chờ URL khác...")

    except websockets.exceptions.ConnectionClosed:
        print("[!] Mất kết nối tới Chrome.")
    except Exception as e:
        print(f"[!] Lỗi khi giao tiếp CDP: {e}")


def main():
    print("=== TIKTOK CDP AUTH POC (TERMUX) ===")
    ws_url = get_tiktok_tab_ws_url()
    if not ws_url:
        return
        
    asyncio.run(monitor_cdp(ws_url))


if __name__ == "__main__":
    main()
