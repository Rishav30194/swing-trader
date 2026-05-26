#!/usr/bin/env python3
"""
migrate_db.py — Migrate trades.db: shares column INTEGER → REAL.

Phase 3 fractional-order update changed Position.shares from int to float.
The database schema must match: shares REAL NOT NULL (was INTEGER NOT NULL).

SQLite does not support ALTER COLUMN TYPE, so migration uses the standard
create-copy-drop-rename approach inside a single transaction.

Note on SQLite type affinity: even with INTEGER affinity, SQLite stores
float values correctly at the data level. This migration is for schema
correctness, not data correctness — existing fractional share values (if
any) are already stored without loss.

Usage:
    python scripts/migrate_db.py                 # uses ./trades.db
    python scripts/migrate_db.py /path/to/db     # explicit path

Creates a .bak backup before migrating. Safe to run on already-migrated DBs.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _shares_column_type(conn: sqlite3.Connection) -> str:
    """Return the declared type of the shares column in positions."""
    for row in conn.execute("PRAGMA table_info(positions)"):
        if row[1] == "shares":
            return row[2].upper()
    raise RuntimeError("shares column not found in positions table")


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"No database found at {db_path} — nothing to migrate.")
        return

    conn = sqlite3.connect(str(db_path))

    try:
        col_type = _shares_column_type(conn)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        conn.close()
        sys.exit(1)

    if col_type == "REAL":
        print(f"shares column is already REAL — no migration needed ({db_path})")
        conn.close()
        return

    print(f"shares column is {col_type} — migrating to REAL …")

    # Backup first
    bak = db_path.with_suffix(".db.bak")
    conn.close()
    shutil.copy2(db_path, bak)
    print(f"Backup created: {bak}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Checkpoint WAL so all data is in the main file before we rewrite the schema
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    conn.executescript("""
        BEGIN;

        CREATE TABLE positions_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT    NOT NULL,
            entry_date      TEXT    NOT NULL,
            entry_price     REAL    NOT NULL,
            shares          REAL    NOT NULL,
            stop_loss       REAL    NOT NULL,
            trailing_stop   REAL    NOT NULL,
            take_profit     REAL    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'open',
            exit_date       TEXT,
            exit_price      REAL,
            exit_reason     TEXT,
            pnl_dollars     REAL,
            pnl_pct         REAL
        );

        INSERT INTO positions_new
            SELECT id, symbol, entry_date, entry_price,
                   CAST(shares AS REAL),
                   stop_loss, trailing_stop, take_profit,
                   status, exit_date, exit_price, exit_reason,
                   pnl_dollars, pnl_pct
            FROM positions;

        DROP TABLE positions;

        ALTER TABLE positions_new RENAME TO positions;

        COMMIT;
    """)

    # Verify
    new_type = _shares_column_type(conn)
    pos_count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    log_count = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    conn.close()

    print(f"Migration complete.")
    print(f"  shares column type : {new_type}")
    print(f"  positions rows     : {pos_count}")
    print(f"  trade_log rows     : {log_count}")

    if new_type != "REAL":
        print("ERROR: migration did not apply correctly — check the backup.")
        sys.exit(1)

    print(f"Done. Backup retained at {bak}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else _ROOT / "trades.db"
    migrate(path)
