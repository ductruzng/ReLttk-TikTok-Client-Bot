import sys

with open('core/proto.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace hardcoded literals
code = code.replace('"web_pc"', '"web"')
code = code.replace('"windows"', '"android"')
code = code.replace('"windows",', '"android",')

with open('core/proto.py', 'w', encoding='utf-8') as f:
    f.write(code)

with open('oneshot.py', 'r', encoding='utf-8') as f:
    oneshot_code = f.read()

# Change User-Agent in oneshot to Android
oneshot_code = oneshot_code.replace(
    '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"',
    '"Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"'
)
with open('oneshot.py', 'w', encoding='utf-8') as f:
    f.write(oneshot_code)

with open('core/api.py', 'r', encoding='utf-8') as f:
    api_code = f.read()

api_code = api_code.replace(
    '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"',
    '"Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"'
)
with open('core/api.py', 'w', encoding='utf-8') as f:
    f.write(api_code)

