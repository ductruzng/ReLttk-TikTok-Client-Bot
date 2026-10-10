"""Explicit fixture setup and simulated clocks for pre-rate-limiter regressions."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
from unittest.mock import patch

import ledger
import ledger_migrations
import ledger_requests


def initialize_fixture(path):
    if not Path(path).exists():
        ledger_migrations.initialize(path)


def fixture_clock(path, date):
    day = date or ledger.get_current_ho_chi_minh_date()
    base = int(datetime.fromisoformat(day + 'T12:00:00+07:00').timestamp() * 1000)
    with closing(sqlite3.connect(path)) as conn:
        last = conn.execute('SELECT max(transmission_started_at_ms) FROM send_requests WHERE date=?', (day,)).fetchone()[0]
    return max(base, (last + 60000) if last is not None else base)


def reserve_at(*args, date=None, db_path, **kwargs):
    initialize_fixture(db_path)
    with patch.object(ledger_requests, 'utc_now_ms', return_value=fixture_clock(db_path, date)):
        return ledger.reserve_pending(*args, date=date, db_path=db_path, **kwargs)


def start_at(*args, date=None, db_path, **kwargs):
    with patch.object(ledger_requests, 'utc_now_ms', return_value=fixture_clock(db_path, date)):
        return ledger_requests.start_transmission(*args, date=date, db_path=db_path, **kwargs)


def migrate_v2_fixture(path):
    with closing(sqlite3.connect(path, isolation_level=None, timeout=30)) as conn:
        ledger_migrations._migrate_v2(conn, path)
