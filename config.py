import os
COOKIES: dict = {}

# Base handshake URL as sent by the TikTok web client (www.tiktok.com/messages).
# access_key and ttwid are appended at runtime by client.build_ws_url; access_key is
# bound to the ttwid/device that issued it, so supply a matching pair via ws_auth.local.json.
WS_URL = (
    "wss://im-ws-sg.tiktok.com/ws/v2"
    "?device_platform=web&version_code=fws_1.0.0"
)
WS_EXTRA_PARAMS = "xsack=1&xaack=1&xsqos=0"
WS_FPID = "9"
WS_AID = "1459"
# Upstream default; only valid together with the upstream author's ttwid
WS_ACCESS_KEY = "277f7a051d7a540326780c413dbc2b9c"
WS_AUTH_FILE = os.environ.get(
    "TIKTOK_WS_AUTH_FILE",
    "ws_auth.local.json"
)

OWN_USER_ID = ""
DEVICE_ID   = ""
VERIFY_FP   = ""

MSG_SDK_MS_TOKEN = ""
TT_PUBLIC_KEY    = ""
TT_CLIENT_DATA   = ""

