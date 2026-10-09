import re

with open('core/proto.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace hardcoded literals with variables
code = code.replace('"web_pc"', 'device_platform')
code = code.replace('"windows"', 'os_name')

# Fix function signatures to include the kwargs at the very end
code = re.sub(
    r'def _build_reaction_request_body\((.*?)\) -> bytes:',
    r'def _build_reaction_request_body(\1, device_platform: str = "web_pc", os_name: str = "windows") -> bytes:',
    code, flags=re.DOTALL
)

code = re.sub(
    r'def _build_request_body\((.*?)\) -> bytes:',
    r'def _build_request_body(\1, device_platform: str = "web_pc", os_name: str = "windows") -> bytes:',
    code, flags=re.DOTALL
)

# For build_ws_packet, it has client_id: str | None = None at the end.
code = re.sub(
    r'def build_ws_packet\((.*?)\) -> tuple\[bytes, int, str\]:',
    r'def build_ws_packet(\1, device_platform: str = "web_pc", os_name: str = "windows") -> tuple[bytes, int, str]:',
    code, flags=re.DOTALL
)

# Inject kwargs into _build_request_body calls
code = re.sub(
    r'request_body = _build_request_body\((.*?)\)',
    r'request_body = _build_request_body(\1, device_platform=device_platform, os_name=os_name)',
    code
)

code = re.sub(
    r'request_body = _build_reaction_request_body\((.*?)\)',
    r'request_body = _build_reaction_request_body(\1, device_platform=device_platform, os_name=os_name)',
    code
)

with open('core/proto.py', 'w', encoding='utf-8') as f:
    f.write(code)
