
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "work" / "tiktok-browser-profile"
TIKTOK_URL = "https://www.tiktok.com/messages"
WS_HOST = "im-ws-sg.tiktok.com"


def has_cookie(cookie_header, cookie_name):
    return any(
        part.strip().split("=", 1)[0].lower() == cookie_name.lower()
        for part in cookie_header.split(";")
        if "=" in part
    )


def main():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    connections = {}
    monitored_pages = set()

    print("=" * 55)
    print("TikTok WebSocket Diagnostics")
    print("=" * 55)
    print("Persistent login: ENABLED")
    print("Sensitive values: HIDDEN")
    print()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 850},
        )

        def attach_cdp(page):
            page_id = id(page)

            if page_id in monitored_pages:
                return

            monitored_pages.add(page_id)

            try:
                cdp = context.new_cdp_session(page)
            except Exception:
                return

            def get_record(event):
                return connections.get(
                    (page_id, event.get("requestId"))
                )

            def websocket_created(event):
                parts = urlsplit(event.get("url", ""))

                if parts.hostname != WS_HOST:
                    return

                params = parse_qs(parts.query)
                key = (page_id, event["requestId"])

                connections[key] = {
                    "access_key_url": bool(
                        params.get("access_key")
                    ),
                    "ttwid_url": bool(params.get("ttwid")),
                    "cookie_header": "Not observed",
                    "ttwid_cookie": "Not observed",
                    "handshake": False,
                    "frames_sent": 0,
                    "frames_received": 0,
                    "frame_error": False,
                    "closed": False,
                }

                print(
                    f"[WS] Connection detected "
                    f"(total: {len(connections)})"
                )

            def handshake_request(event):
                record = get_record(event)

                if record is None:
                    return

                headers = event.get(
                    "request", {}
                ).get("headers", {})

                cookie = next(
                    (
                        str(value)
                        for name, value in headers.items()
                        if name.lower() == "cookie"
                    ),
                    None,
                )

                if cookie is not None:
                    record["cookie_header"] = True
                    record["ttwid_cookie"] = has_cookie(
                        cookie, "ttwid"
                    )

            def handshake_response(event):
                record = get_record(event)

                if record is None:
                    return

                record["handshake"] = (
                    event.get("response", {}).get("status")
                    == 101
                )

            def frame_sent(event):
                record = get_record(event)

                if record is not None:
                    record["frames_sent"] += 1

            def frame_received(event):
                record = get_record(event)

                if record is not None:
                    record["frames_received"] += 1

            def frame_error(event):
                record = get_record(event)

                if record is not None:
                    record["frame_error"] = True

            def websocket_closed(event):
                record = get_record(event)

                if record is not None:
                    record["closed"] = True

            cdp.on(
                "Network.webSocketCreated",
                websocket_created,
            )
            cdp.on(
                "Network.webSocketWillSendHandshakeRequest",
                handshake_request,
            )
            cdp.on(
                "Network.webSocketHandshakeResponseReceived",
                handshake_response,
            )
            cdp.on(
                "Network.webSocketFrameSent",
                frame_sent,
            )
            cdp.on(
                "Network.webSocketFrameReceived",
                frame_received,
            )
            cdp.on(
                "Network.webSocketFrameError",
                frame_error,
            )
            cdp.on(
                "Network.webSocketClosed",
                websocket_closed,
            )

            cdp.send("Network.enable")

        def print_report():
            print("\n" + "=" * 55)
            print("WEBSOCKET DIAGNOSTICS")
            print("=" * 55)
            print("Detected:", len(connections))

            qualified = 0

            for index, record in enumerate(
                connections.values(), 1
            ):
                print(f"\nConnection #{index}")
                print(
                    "  access_key in URL:",
                    record["access_key_url"],
                )
                print(
                    "  ttwid in URL:",
                    record["ttwid_url"],
                )
                print(
                    "  Cookie header observed:",
                    record["cookie_header"],
                )
                print(
                    "  ttwid in Cookie header:",
                    record["ttwid_cookie"],
                )
                print(
                    "  Handshake 101:",
                    record["handshake"],
                )
                print(
                    "  Frames sent:",
                    record["frames_sent"],
                )
                print(
                    "  Frames received:",
                    record["frames_received"],
                )
                print(
                    "  Frame error:",
                    record["frame_error"],
                )
                print(
                    "  Closed:",
                    record["closed"],
                )

                if (
                    record["access_key_url"]
                    and record["handshake"]
                    and record["frames_sent"] > 0
                    and record["frames_received"] > 0
                    and not record["frame_error"]
                ):
                    qualified += 1

            print("\nActive-data connections:", qualified)
            print("=" * 55)

        try:
            context.on("page", attach_cdp)

            for existing_page in context.pages:
                attach_cdp(existing_page)

            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            page.goto(
                TIKTOK_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            print("[+] Browser opened.")
            print("[+] Monitoring WebSocket for 30 seconds.")
            print("[+] Reload Messages if needed.")

            # Allows Playwright to process CDP events.
            page.wait_for_timeout(30000)

            print_report()

            input("\nPress Enter to close browser...")

        finally:
            context.close()
            print("[+] Browser closed. Login profile saved.")


if __name__ == "__main__":
    main()
