# database.py
"""
===========================================================
 DATABASE (SQLite) - schema + auto-migration
===========================================================
BUG FIX: tracker.py target_1/target_2/target_3/t1_hit/t2_hit/t3_hit
columns use karta tha, lekin CREATE TABLE mein ye columns kabhi
the hi nahi (sirf purana 'target_price' tha). Isse har INSERT aur
SELECT * ke baad column access silently fail ho raha tha - matlab
koi bhi recommendation kabhi DB mein save hi nahi ho rahi thi, aur
--monitor / --report hamesha "no such column" error de rahe the.

Fix: naye columns schema mein add kiye, aur agar purana
trading_system.db file already exist karta hai (jisme ye columns
nahi hain), to use automatically ALTER TABLE se migrate kar diya
jaata hai - purana data delete nahi hota.

V8.2.0 FIXES:
- WAL mode + busy_timeout: pehle default rollback-journal + 0
  busy_timeout use hota tha - concurrent writes (tracker monitor +
  scanner writes + bot_listener reads) thread-safety ke saath
  immediately "database is locked" de dete the. Ab WAL mode enable
  kar ke (multiple readers + 1 writer concurrency) aur busy_timeout
  5000ms set karke wait karte hain, error se nahi.
- init_db() guard: har import pe re-run nahi hota - module-level
  _initialized flag.
- db_transaction() context manager: open -> yield -> commit/rollback
  -> close automatically. tracker.py ke 8+ open/commit/close pattern
  ke liye cleaner + safer (exception pe auto rollback).
===========================================================
"""

import sqlite3
import os
import threading
from contextlib import contextmanager

from logger import logger

DB_NAME = "trading_system.db"

# V8.2.0: init_db() ko har import pe re-run hone se rokne ke liye.
# Pehle har consumer (tracker.py, master_dashboard.py, bot_listener.py)
# import pe re-CREATE/ALTER chalata tha - 3x wasteful.
_initialized = False
_init_lock = threading.Lock()


def get_db_connection():
    """
    Database connection create aur return karne ke liye.

    V8.2.0: WAL mode + busy_timeout enable karte hain (network FS
    par WAL fail ho sakta hai, isliye try/except mein - uss case
    mein default rollback-journal use hoga).
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    # WAL (Write-Ahead Logging) mode: multiple concurrent readers +
    # single writer - SQLite ke "database is locked" errors bahut
    # kam hote hain. busy_timeout: agar lock milne tak wait karo
    # 5000ms (5s) instead of immediate OperationalError.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as e:
        # WAL network filesystems (NFS/CIFS) par support nahi hota -
        # default rollback-journal wahi rahega. Log karke aage badho.
        logger.debug(f"WAL mode enable nahi ho paya (rollback-journal use hoga): {e}")
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError as e:
        logger.debug(f"busy_timeout set nahi ho paya: {e}")
    # V8.2.0: foreign_keys ON - schema aage foreign-key constraints
    # add kare to enable ho (defensive, abhi koi FK nahi hai).
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.OperationalError:
        pass
    return conn


@contextmanager
def db_transaction():
    """
    V8.2.0: Context manager - opens connection, yields cursor,
    commits on success, rolls back on exception, closes connection.

    Usage:
        with db_transaction() as cur:
            cur.execute("INSERT INTO ...", (val1, val2))
            cur.execute("UPDATE ...", (val3,))

    Safe against exceptions - koi connection leak nahi hota, aur
    partial writes automatically rollback ho jaate hain.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _existing_columns(cursor, table):
    # NOTE: PRAGMA table_info() sqlite3 mein `?` parameter binding
    # support nahi karta - isliye f-string use kiya. Safe hai kyunki
    # `table` sirf hardcoded internal values se aata hai (kabhi user
    # input se nahi).
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def init_db():
    """Database Tables create karne ke liye (Sirf pehli baar chalega) + auto-migration"""
    global _initialized
    # V8.2.0: har import pe re-run hone se rokne ke liye flag. Thread-safe
    # check (bot_listener + scheduler dono simultaneously import kar sakte hain).
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock TEXT NOT NULL,
                date_added TEXT NOT NULL,
                entry_price REAL,
                entry_low REAL,
                entry_high REAL,
                sl_price REAL,
                target_1 REAL,
                target_2 REAL,
                target_3 REAL,
                t1_hit INTEGER DEFAULT 0,
                t2_hit INTEGER DEFAULT 0,
                t3_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                closed_date TEXT,
                closed_price REAL,
                signal TEXT,
                score INTEGER,
                patterns TEXT
            )
        ''')
        conn.commit()

        # ---- MIGRATION: agar purana table already exist karta tha bina
        # naye columns ke, to yahan add kar dete hain (data safe rehta hai) ----
        required_columns = {
            "target_1": "REAL", "target_2": "REAL", "target_3": "REAL",
            "t1_hit": "INTEGER DEFAULT 0", "t2_hit": "INTEGER DEFAULT 0", "t3_hit": "INTEGER DEFAULT 0",
            "entry_low": "REAL", "entry_high": "REAL",
        }
        existing = _existing_columns(cursor, "recommendations")
        migrated = []
        for col, col_type in required_columns.items():
            if col not in existing:
                # NOTE: ALTER TABLE ADD COLUMN bhi `?` binding support
                # nahi karta - f-string safe hai (hardcoded internal values).
                cursor.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {col_type}")
                migrated.append(col)

        # Purana 'target_price' column agar tha aur naya target_1 khaali hai,
        # to ek-baari copy kar dete hain taaki purani recommendations bhi
        # tracker mein dikhein (data loss nahi hona chahiye)
        if "target_price" in existing and "target_1" in required_columns:
            try:
                cursor.execute(
                    "UPDATE recommendations SET target_1 = target_price, target_2 = target_price, "
                    "target_3 = target_price WHERE target_1 IS NULL AND target_price IS NOT NULL"
                )
            except Exception as e:
                logger.warning(f"Purane target_price se migration mein warning: {e}")

        conn.commit()
        conn.close()

        if migrated:
            logger.info(f"Database migrate ho gaya, naye columns add hue: {migrated}")
        logger.info("SQLite Database and Tables Initialized Successfully.")
        _initialized = True


# Script run hote hi database check aur init ho jayega
# (V8.2.0: _initialized flag ke saath - pehli import pe hi chalega,
# baaki imports fast-path skip ho jaate hain)
init_db()
