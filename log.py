import os
import re
import sys

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

_R  = "" if _NO_COLOR else "\033[0m"
_DIM = "" if _NO_COLOR else "\033[2m"

_CYAN    = "" if _NO_COLOR else "\033[96m"
_GREEN   = "" if _NO_COLOR else "\033[92m"
_YELLOW  = "" if _NO_COLOR else "\033[93m"
_RED     = "" if _NO_COLOR else "\033[91m"
_BLUE    = "" if _NO_COLOR else "\033[94m"
_MAGENTA = "" if _NO_COLOR else "\033[95m"
_WHITE   = "" if _NO_COLOR else "\033[97m"
_BOLD    = "" if _NO_COLOR else "\033[1m"

# Sensitive token / credential patterns for sink redaction
_RE_URL_QUERY = re.compile(r'(?i)\b((?:https?|wss)://[^\s?#]+)(\?[^\s#]*)')
_RE_COOKIE_HDR = re.compile(r'''(?i)(['"]?cookie['"]?\s*[:=]\s*)(['"][^'"]*['"]|[^\r\n,}\]]+)''')
_RE_SENSITIVE_KEYS = re.compile(
    r'''(?i)(['"]?(?:sessionid(?:_[a-z0-9]+)?|sid_tt(?:_[a-z0-9]+)?|uid_tt(?:_[a-z0-9]+)?|'''
    r'''mstoken|ttwid|web-sdk-ms-token|passport_ticket|x-mssdk-info|access_key|client_secret|'''
    r'''sid_guard|csrf_token|tt_csrf_token|\btoken)['"]?\s*[:=]\s*)(['"][^'"]*['"]|[^\s;,"'&}\]]+)'''
)


def redact(text: str) -> str:
    """Sanitize secrets, tokens, cookies, and sensitive URLs from log strings."""
    if not isinstance(text, str):
        text = str(text)

    # 1. Sanitize full URL query strings for http, https, and wss
    text = _RE_URL_QUERY.sub(r'\1?[REDACTED]', text)

    # 2. Sanitize Cookie headers and dict representations
    def _sub_cookie(m):
        prefix = m.group(1)
        val = m.group(2)
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            q = val[0]
            return f"{prefix}{q}[REDACTED]{q}"
        return f"{prefix}[REDACTED]"

    text = _RE_COOKIE_HDR.sub(_sub_cookie, text)

    # 3. Sanitize sensitive keys, variants, and short values
    def _sub_key(m):
        prefix = m.group(1)
        val = m.group(2)
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            q = val[0]
            return f"{prefix}{q}[REDACTED]{q}"
        return f"{prefix}[REDACTED]"

    text = _RE_SENSITIVE_KEYS.sub(_sub_key, text)

    return text


def _tag(color: str, tag: str) -> str:
    return f"{color}{_BOLD}[{tag}]{_R}"


def info(tag: str, msg: str):
    print(f"{_tag(_CYAN, tag)} {redact(msg)}")


def ok(tag: str, msg: str):
    print(f"{_tag(_GREEN, tag)} {_GREEN}{redact(msg)}{_R}")


def warn(tag: str, msg: str):
    print(f"{_tag(_YELLOW, tag)} {_YELLOW}{redact(msg)}{_R}")


def error(tag: str, msg: str):
    print(f"{_tag(_RED, tag)} {_RED}{redact(msg)}{_R}")


def msg(ts: str, sender: str, group: str, text: str):
    ts_part     = f"{_DIM}[{ts}]{_R}"
    group_part  = f" {_MAGENTA}[{group}]{_R}" if group else ""
    sender_part = f"{_BOLD}{sender}{_R}"
    print(f"{ts_part}{group_part} {sender_part}: {redact(text)}")


def reaction(ts: str, sender: str, action: str, emoji: str):
    ts_part = f"{_DIM}[{ts}]{_R}"
    print(f"{ts_part} {_BOLD}{sender}{_R}: [{_YELLOW}{action} {emoji}{_R}]")


def plugin(tag: str, action: str, name: str):
    color = _GREEN if action == "nuevo" else _RED if action == "eliminado" else _YELLOW
    print(f"{_tag(_CYAN, tag)} plugin {color}{action}{_R}: {redact(name)}")
