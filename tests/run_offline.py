"""Reviewer runner: prohibit external networking and real runtime-data access."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
protected = [ROOT / p for p in (
    'sesion', 'sessions', 'state', 'ws_auth.local.json', 'streak.local.json',
    '.uid_cache.json', 'work/tiktok-browser-profile', 'work/ws_auth.candidate.local.json',
    'work/tui-settings.local.json',
)]

def guard(event, args):
    if event == 'socket.getaddrinfo':
        raise RuntimeError('Review blocked external DNS')
    if event == 'socket.connect' and args[1][0] not in ('127.0.0.1', '::1'):
        raise RuntimeError('Review blocked external connection')
    if event in ('open', 'sqlite3.connect') and isinstance(args[0], (str, bytes, os.PathLike)):
        raw = os.fsdecode(args[0])
        if raw.startswith('file:'):
            from urllib.parse import unquote, urlsplit
            raw = unquote(urlsplit(raw).path)
            if os.name == 'nt' and len(raw) > 2 and raw[0] == '/' and raw[2] == ':':
                raw = raw[1:]
        path = Path(raw).absolute()
        if any(path == p or p in path.parents for p in protected):
            raise RuntimeError('Review blocked real runtime data')

sys.addaudithook(guard)
if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('Offline guard: external network and real runtime files blocked.')
    raise SystemExit(not result.wasSuccessful())
