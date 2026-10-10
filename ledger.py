import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime
from typing import Tuple, Optional

# Timezone support: Asia/Ho_Chi_Minh (UTC+7)
try:
    import zoneinfo
    TZ_HO_CHI_MINH = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    from datetime import timezone, timedelta
    TZ_HO_CHI_Minh = timezone(timedelta(hours=7))
    TZ_HO_CHI_MINH = TZ_HO_CHI_Minh

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED_UNKNOWN = "failed_unknown"
STATUS_BLOCKED_CROSSOVER = "blocked_crossover"

# Fixed reason codes to prevent leaking raw error messages, URLs, or tokens
REASON_CONFIRMED = "CONFIRMED_ECHO"
REASON_TIMEOUT = "TIMEOUT"
REASON_CORRELATION_TIMEOUT = "CORRELATION_TIMEOUT"
REASON_DISCONNECTED = "DISCONNECTED"
REASON_WS_ERROR = "WS_ERROR"
REASON_AUTH_ERROR = "AUTH_ERROR"
REASON_CLIENT_EXCEPTION = "CLIENT_EXCEPTION"
REASON_CROSSOVER_GUARD = "CROSSOVER_GUARD"
REASON_MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
REASON_UNKNOWN = "UNKNOWN"

ALLOWED_REASON_CODES = {
    REASON_CONFIRMED,
    REASON_TIMEOUT,
    REASON_CORRELATION_TIMEOUT,
    REASON_DISCONNECTED,
    REASON_WS_ERROR,
    REASON_AUTH_ERROR,
    REASON_CLIENT_EXCEPTION,
    REASON_CROSSOVER_GUARD,
    REASON_MALFORMED_RESPONSE,
    REASON_UNKNOWN,
}

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_STATE_DIR = os.path.join(_REPO_DIR, "state")
_DEFAULT_LEDGER_PATH = os.path.join(_DEFAULT_STATE_DIR, "streak_ledger.db")
_DEFAULT_LOCK_PATH = os.path.join(_DEFAULT_STATE_DIR, "run.lock")


class LedgerError(Exception):
    """Base exception for daily ledger operations."""
    pass


class QuotaExceededError(LedgerError):
    """Raised when an attempt has already been reserved or confirmed today."""
    pass


class SchemaError(LedgerError, ValueError):
    """Safe local diagnostic, suitable for CLI display without credential values."""


class LockError(LedgerError):
    """Raised when the process run lock cannot be acquired."""
    pass


def get_current_ho_chi_minh_date(dt: Optional[datetime] = None) -> str:
    """Return YYYY-MM-DD date formatted for Asia/Ho_Chi_Minh timezone."""
    if dt is None:
        dt = datetime.now(TZ_HO_CHI_MINH)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_HO_CHI_MINH)
    else:
        dt = dt.astimezone(TZ_HO_CHI_MINH)
    return dt.strftime("%Y-%m-%d")


def _sanitize_reason_code(code: Optional[str]) -> str:
    """Ensure reason code is strictly one of the allowed fixed codes."""
    if code in ALLOWED_REASON_CODES:
        return code
    return REASON_UNKNOWN


def _secure_state_dir(dir_path: str) -> None:
    """Ensure private state directory exists and tighten permissions (0700 on POSIX).

    POSIX chmod errors fail closed.
    Note: On Windows, chmod does not modify NTFS ACLs; Windows security relies
    on standard user profile ACL isolation.
    """
    if os.path.exists(dir_path) and os.path.islink(dir_path):
        raise PermissionError(f"Symlinks are rejected for state directory: {dir_path}")
    os.makedirs(dir_path, mode=0o700, exist_ok=True)
    if hasattr(os, "chmod") and sys.platform != "win32":
        os.chmod(dir_path, 0o700)


def _secure_file_permissions(path: str) -> None:
    """Tighten SQLite database file permissions (0600 on POSIX).

    POSIX chmod errors fail closed.
    Note: On Windows, chmod does not modify NTFS ACLs; Windows security relies
    on standard user profile ACL isolation.
    """
    if os.path.islink(path):
        raise PermissionError(f"Symlinks are rejected for ledger file: {path}")
    if hasattr(os, "chmod") and sys.platform != "win32":
        os.chmod(path, 0o600)


def _init_db(conn: sqlite3.Connection) -> None:
    """Initialize SQLite schema with concurrency pragmas and constraints."""
    conn.execute("PRAGMA busy_timeout = 30000")
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode")
    row = cur.fetchone()
    if not row or str(row[0]).lower() != "wal":
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
    # synchronous = FULL ensures durable fsync before network send
    conn.execute("PRAGMA synchronous = FULL")



def _get_readwrite_connection(db_path: str = _DEFAULT_LEDGER_PATH) -> sqlite3.Connection:
    """Open an existing V5 database; sending never initializes or migrates."""
    if not os.path.isfile(db_path) or os.path.islink(db_path):
        raise LedgerError("Ledger missing or unsafe; explicit ledger-initialize is required")
    conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=rw", uri=True,
                           timeout=30.0, isolation_level=None)
    try:
        from ledger_migrations import validate_schema
        try:
            validate_schema(conn)
        except ValueError as exc:
            raise SchemaError(str(exc)) from exc
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except BaseException:
        conn.close()
        raise


def check_ready(db_path=_DEFAULT_LEDGER_PATH):
    """Read-only schema gate to run before loading credentials or accessing TikTok."""
    conn = _get_readonly_connection(db_path)
    if conn is None:
        raise LedgerError("Ledger missing; run explicit ledger-initialize with the intended path")
    try:
        from ledger_migrations import validate_schema
        try:
            validate_schema(conn)
        except ValueError as exc:
            raise SchemaError(str(exc)) from exc
    finally:
        conn.close()



def _get_readonly_connection(db_path: str = _DEFAULT_LEDGER_PATH) -> Optional[sqlite3.Connection]:
    """Open strictly readonly SQLite connection without creating database file or directory.

    Returns None if the database file does not exist.
    """
    if not os.path.exists(db_path):
        return None
    if os.path.islink(db_path):
        raise PermissionError(f"Symlinks are rejected for ledger database: {db_path}")

    # URI encoding also handles spaces, '#' and '?' in fixture paths.
    conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    return conn


def is_already_attempted(
    canonical_uid: str,
    conv_id: str,
    date: Optional[str] = None,
    db_path: str = _DEFAULT_LEDGER_PATH
) -> Tuple[bool, Optional[str]]:
    """Check if an attempt for (canonical_uid, conv_id, date) has already been recorded.

    Returns (already_attempted, status).
    Safe for dry-run inspection: strictly read-only, does NOT create DB file or directory.
    """
    if not os.path.exists(db_path):
        return False, None

    if date is None:
        date = get_current_ho_chi_minh_date()

    conn = _get_readonly_connection(db_path)
    if conn is None:
        return False, None

    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version in (2, 3, 4, 5):
            row = conn.execute("SELECT status FROM send_requests WHERE canonical_uid=? AND conv_id=? AND date=? AND transmitted=1 ORDER BY attempt_time DESC LIMIT 1",
                               (str(canonical_uid), str(conv_id), str(date))).fetchone()
            if row:
                return True, row[0]
            if conn.execute("SELECT 1 FROM crossover_guards WHERE canonical_uid=? AND conv_id=? AND date=?", (str(canonical_uid), str(conv_id), str(date))).fetchone():
                return True, STATUS_BLOCKED_CROSSOVER
        else:
            row = conn.execute("SELECT status FROM daily_ledger WHERE canonical_uid=? AND conv_id=? AND date=?", (str(canonical_uid), str(conv_id), str(date))).fetchone()
            if row:
                return True, row[0]
        return False, None
    finally:
        conn.close()


def reserve_pending(canonical_uid, conv_id, client_msg_id, date=None, db_path=_DEFAULT_LEDGER_PATH):
    """Compatibility adapter: a conservative transmission reservation, never force-delete."""
    from ledger_requests import create_request, start_transmission
    day = date or get_current_ho_chi_minh_date()
    request, created = create_request(canonical_uid, conv_id, "legacy-api:" + client_msg_id,
                                      {"client_msg_id": client_msg_id}, db_path=db_path,
                                      client_msg_id=client_msg_id)
    if not created:
        raise QuotaExceededError("Reservation already exists")
    start_transmission(request['request_id'], db_path=db_path, date=day)
    return day


def confirm_send(canonical_uid, conv_id, send_date, client_msg_id, server_msg_id,
                 confirm_date=None, db_path=_DEFAULT_LEDGER_PATH):
    from ledger_requests import finish
    if not all((canonical_uid, conv_id, send_date, client_msg_id)):
        raise ValueError("Reservation identity is required")
    try:
        sid = int(str(server_msg_id))
        if sid <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("server_msg_id must be a positive integer")
    finish(canonical_uid, conv_id, send_date, client_msg_id, db_path=db_path,
           status=STATUS_CONFIRMED, reason=REASON_CONFIRMED, server_id=str(sid),
           result_date=confirm_date or get_current_ho_chi_minh_date())


def mark_failed_unknown(canonical_uid, conv_id, send_date, client_msg_id,
                        reason_code=REASON_UNKNOWN, failure_date=None, db_path=_DEFAULT_LEDGER_PATH, failure=None):
    from ledger_requests import finish
    finish(canonical_uid, conv_id, send_date, client_msg_id, db_path=db_path,
           status=STATUS_FAILED_UNKNOWN, reason=_sanitize_reason_code(reason_code), failure=failure,
           result_date=failure_date or get_current_ho_chi_minh_date())


class RunLock:
    """Process-level run lock to prevent concurrent CLI runs from racing across reservation.

    Uses msvcrt locking on Windows and fcntl.flock on POSIX.
    """
    def __init__(self, lock_path: str = _DEFAULT_LOCK_PATH):
        self.lock_path = lock_path
        self._fd: Optional[int] = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def acquire(self) -> None:
        state_dir = os.path.dirname(os.path.abspath(self.lock_path))
        _secure_state_dir(state_dir)

        if os.path.exists(self.lock_path) and os.path.islink(self.lock_path):
            raise PermissionError(f"Symlinks are rejected for run lock: {self.lock_path}")

        if sys.platform == "win32":
            import msvcrt
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)
            try:
                os.write(self._fd, b"L")
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            except OSError as e:
                os.close(self._fd)
                self._fd = None
                raise LockError(f"Could not acquire run lock: another process is active ({e})")
        else:
            import fcntl
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as e:
                os.close(self._fd)
                self._fd = None
                raise LockError(f"Could not acquire run lock: another process is active ({e})")

    def release(self) -> None:
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(self._fd)
            finally:
                self._fd = None
