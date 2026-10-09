import os
import sqlite3
import sys
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_ledger (
            canonical_uid TEXT NOT NULL,
            conv_id TEXT NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_time TEXT NOT NULL,
            client_msg_id TEXT NOT NULL,
            server_msg_id TEXT,
            confirmed_time TEXT,
            reason_code TEXT,
            PRIMARY KEY (canonical_uid, conv_id, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_lookup ON daily_ledger(canonical_uid, conv_id, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_pending ON daily_ledger(canonical_uid, conv_id, status)")
    conn.commit()


def _get_readwrite_connection(db_path: str = _DEFAULT_LEDGER_PATH) -> sqlite3.Connection:
    """Open read-write database connection, initializing file and tables if needed."""
    if os.path.exists(db_path) and os.path.islink(db_path):
        raise PermissionError(f"Symlinks are rejected for ledger database: {db_path}")

    parent_dir = os.path.dirname(os.path.abspath(db_path))
    _secure_state_dir(parent_dir)

    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    _init_db(conn)
    _secure_file_permissions(db_path)
    return conn


def _get_readonly_connection(db_path: str = _DEFAULT_LEDGER_PATH) -> Optional[sqlite3.Connection]:
    """Open strictly readonly SQLite connection without creating database file or directory.

    Returns None if the database file does not exist.
    """
    if not os.path.exists(db_path):
        return None
    if os.path.islink(db_path):
        raise PermissionError(f"Symlinks are rejected for ledger database: {db_path}")

    abs_path = os.path.abspath(db_path)
    # Use SQLite URI mode=ro to ensure no file or sidecar creation
    conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True, timeout=10.0)
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
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM daily_ledger WHERE canonical_uid = ? AND conv_id = ? AND date = ?",
            (str(canonical_uid), str(conv_id), str(date))
        )
        row = cur.fetchone()
        if row:
            return True, row[0]
        return False, None
    finally:
        conn.close()


def reserve_pending(
    canonical_uid: str,
    conv_id: str,
    client_msg_id: str,
    date: Optional[str] = None,
    db_path: str = _DEFAULT_LEDGER_PATH
) -> str:
    """Atomically claim durable pending status before initiating any network send.

    Recomputes the current date in Asia/Ho_Chi_Minh immediately before reservation.
    Uses PRAGMA synchronous = FULL so reservation is guaranteed durable against crashes.
    Raises QuotaExceededError if an attempt has already been reserved or confirmed today,
    or if an unresolved pending reservation exists across dates (crash guard).
    Returns the reserved date string (YYYY-MM-DD).
    """
    if not canonical_uid:
        raise ValueError("canonical_uid is required for reservation")
    if not conv_id:
        raise ValueError("conv_id is required for reservation")
    if not client_msg_id:
        raise ValueError("client_msg_id is required for reservation")

    send_date = date if date is not None else get_current_ho_chi_minh_date()
    now_iso = datetime.now(TZ_HO_CHI_MINH).isoformat()

    conn = _get_readwrite_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # 1. Crash guard: check for any unresolved pending reservation across dates
            cur.execute(
                "SELECT date, client_msg_id FROM daily_ledger WHERE canonical_uid = ? AND conv_id = ? AND status = ?",
                (str(canonical_uid), str(conv_id), STATUS_PENDING)
            )
            pending_row = cur.fetchone()
            if pending_row:
                cur.execute("ROLLBACK")
                raise QuotaExceededError(
                    f"Target '{conv_id}' has an unresolved pending reservation from {pending_row[0]} ({pending_row[1]}). "
                    f"Conservative crash guard blocks further sends across dates until manual review."
                )

            # 2. Check for existing record on target send_date
            cur.execute(
                "SELECT status FROM daily_ledger WHERE canonical_uid = ? AND conv_id = ? AND date = ?",
                (str(canonical_uid), str(conv_id), str(send_date))
            )
            row = cur.fetchone()
            if row:
                cur.execute("ROLLBACK")
                raise QuotaExceededError(
                    f"Target '{conv_id}' on date '{send_date}' already has recorded status '{row[0]}'. "
                    f"Daily quota prevents further attempts today."
                )

            cur.execute("""
                INSERT INTO daily_ledger (canonical_uid, conv_id, date, status, attempt_time, client_msg_id, reason_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (str(canonical_uid), str(conv_id), str(send_date), STATUS_PENDING, now_iso, str(client_msg_id), None))
            cur.execute("COMMIT")
            return send_date
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()


def confirm_send(
    canonical_uid: str,
    conv_id: str,
    send_date: str,
    client_msg_id: str,
    server_msg_id: str,
    confirm_date: Optional[str] = None,
    db_path: str = _DEFAULT_LEDGER_PATH
) -> None:
    """Record verified server confirmation.

    Validates that:
    1. A pending reservation exists for (canonical_uid, conv_id, send_date).
    2. The record's client_msg_id matches the provided client UUID.
    3. server_msg_id is a valid positive integer.

    Handles midnight crossover conservatively: if server response arrives on a new calendar day,
    the reservation day is confirmed and the subsequent day is conservatively guarded with
    STATUS_BLOCKED_CROSSOVER using INSERT OR IGNORE (never overwriting an existing record).
    """
    if not canonical_uid or not conv_id or not send_date or not client_msg_id:
        raise ValueError("canonical_uid, conv_id, send_date, and client_msg_id are all required")

    try:
        sid_int = int(str(server_msg_id))
        if sid_int <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(f"server_msg_id must be a positive integer, got: {server_msg_id!r}")

    if confirm_date is None:
        confirm_date = get_current_ho_chi_minh_date()
    now_iso = datetime.now(TZ_HO_CHI_MINH).isoformat()

    conn = _get_readwrite_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "SELECT status, client_msg_id FROM daily_ledger WHERE canonical_uid = ? AND conv_id = ? AND date = ?",
                (str(canonical_uid), str(conv_id), str(send_date))
            )
            row = cur.fetchone()
            if not row:
                cur.execute("ROLLBACK")
                raise LedgerError(
                    f"No reservation record found for {conv_id} on {send_date}. Cannot confirm without reservation."
                )

            recorded_status, recorded_client_id = row[0], row[1]
            if recorded_status != STATUS_PENDING:
                cur.execute("ROLLBACK")
                raise LedgerError(
                    f"Reservation record for {conv_id} on {send_date} has status '{recorded_status}', expected '{STATUS_PENDING}'."
                )

            if recorded_client_id != str(client_msg_id):
                cur.execute("ROLLBACK")
                raise LedgerError(
                    f"client_msg_id mismatch: reservation has '{recorded_client_id}', attempted confirmation with '{client_msg_id}'."
                )

            cur.execute("""
                UPDATE daily_ledger
                SET status = ?, server_msg_id = ?, confirmed_time = ?, reason_code = ?
                WHERE canonical_uid = ? AND conv_id = ? AND date = ? AND status = ? AND client_msg_id = ?
            """, (
                STATUS_CONFIRMED, str(sid_int), now_iso, REASON_CONFIRMED,
                str(canonical_uid), str(conv_id), str(send_date), STATUS_PENDING, str(client_msg_id)
            ))

            if cur.rowcount != 1:
                cur.execute("ROLLBACK")
                raise LedgerError("Failed to update pending reservation record to confirmed.")

            # Midnight crossover handling: do NOT destroy an existing record with INSERT OR REPLACE.
            # Use INSERT OR IGNORE so existing records on the subsequent date are preserved.
            if confirm_date != send_date:
                cur.execute("""
                    INSERT OR IGNORE INTO daily_ledger
                    (canonical_uid, conv_id, date, status, attempt_time, client_msg_id, server_msg_id, confirmed_time, reason_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(canonical_uid), str(conv_id), str(confirm_date),
                    STATUS_BLOCKED_CROSSOVER, now_iso, str(client_msg_id),
                    str(sid_int), now_iso, REASON_CROSSOVER_GUARD
                ))

            cur.execute("COMMIT")
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()


def mark_failed_unknown(
    canonical_uid: str,
    conv_id: str,
    send_date: str,
    client_msg_id: str,
    reason_code: str = REASON_UNKNOWN,
    failure_date: Optional[str] = None,
    db_path: str = _DEFAULT_LEDGER_PATH
) -> None:
    """Record timeout, error, or unconfirmed attempt as failed_unknown.

    Blocks further attempts for the day (no automatic retries).
    Persists safe fixed reason code only (no raw exception strings, URLs, or tokens).
    Cannot overwrite an already confirmed record.
    If midnight crossed, conservative guard is placed on subsequent date with INSERT OR IGNORE.
    """
    safe_code = _sanitize_reason_code(reason_code)
    if failure_date is None:
        failure_date = get_current_ho_chi_minh_date()
    now_iso = datetime.now(TZ_HO_CHI_MINH).isoformat()

    conn = _get_readwrite_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "SELECT status FROM daily_ledger WHERE canonical_uid = ? AND conv_id = ? AND date = ?",
                (str(canonical_uid), str(conv_id), str(send_date))
            )
            row = cur.fetchone()

            # A confirmed record must NEVER be overwritten by failure
            if row and row[0] == STATUS_CONFIRMED:
                cur.execute("ROLLBACK")
                return

            if row:
                cur.execute("""
                    UPDATE daily_ledger
                    SET status = ?, reason_code = ?
                    WHERE canonical_uid = ? AND conv_id = ? AND date = ? AND status != ?
                """, (STATUS_FAILED_UNKNOWN, safe_code, str(canonical_uid), str(conv_id), str(send_date), STATUS_CONFIRMED))
            else:
                cur.execute("""
                    INSERT OR IGNORE INTO daily_ledger
                    (canonical_uid, conv_id, date, status, attempt_time, client_msg_id, reason_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(canonical_uid), str(conv_id), str(send_date),
                    STATUS_FAILED_UNKNOWN, now_iso, str(client_msg_id), safe_code
                ))

            if failure_date != send_date:
                cur.execute("""
                    INSERT OR IGNORE INTO daily_ledger
                    (canonical_uid, conv_id, date, status, attempt_time, client_msg_id, reason_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(canonical_uid), str(conv_id), str(failure_date),
                    STATUS_BLOCKED_CROSSOVER, now_iso, str(client_msg_id), REASON_CROSSOVER_GUARD
                ))

            cur.execute("COMMIT")
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()


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
