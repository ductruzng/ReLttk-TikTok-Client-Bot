import sys

with open('core/proto.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('"web_pc"', 'device_platform')
content = content.replace('"windows"', 'os_name')

content = content.replace(
    'def _build_request_body(msg_body: bytes, device_id: str, sdk_ms_token: str,',
    'def _build_request_body(msg_body: bytes, device_id: str, sdk_ms_token: str, device_platform: str = "web_pc", os_name: str = "windows",'
)
content = content.replace(
    'def _build_reaction_request_body(react_body: bytes, device_id: str, sdk_ms_token: str,',
    'def _build_reaction_request_body(react_body: bytes, device_id: str, sdk_ms_token: str, device_platform: str = "web_pc", os_name: str = "windows",'
)
content = content.replace(
    'request_body = _build_request_body(msg_body, device_id, sdk_ms_token,',
    'request_body = _build_request_body(msg_body, device_id, sdk_ms_token, device_platform=device_platform, os_name=os_name,'
)
content = content.replace(
    'request_body = _build_reaction_request_body(react_body, device_id, sdk_ms_token,',
    'request_body = _build_reaction_request_body(react_body, device_id, sdk_ms_token, device_platform=device_platform, os_name=os_name,'
)
content = content.replace(
    'def build_ws_packet(',
    'def build_ws_packet(\n    device_platform: str = "web_pc",\n    os_name: str = "windows",'
)
# Ensure we don't break default params inside _build_msg_body, wait, _build_msg_body doesn't take device_platform.
# Let's write the content back.

with open('core/proto.py', 'w', encoding='utf-8') as f:
    f.write(content)
