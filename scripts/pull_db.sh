#!/usr/bin/env bash
# pull_db.sh — Pull a clean snapshot of trades.db from the VPS.
#
# Usage:
#   bash scripts/pull_db.sh
#
# What it does:
#   1. Checkpoints the WAL on the VPS so all data is in the main file
#   2. SCPs trades.db to the project root (gitignored)
#   3. Prints row counts for a quick sanity check
#
# Run this any time you want to review trades in DB Browser for SQLite.
# After running, open DB Browser and hit File → Revert to reload.

set -euo pipefail

VPS_USER="trader"
VPS_HOST="5.78.207.143"
VPS_DB="/home/trader/swing-trader/trades.db"
LOCAL_DB="$(cd "$(dirname "$0")/.." && pwd)/trades.db"

echo "→ Checkpointing WAL on VPS..."
ssh "${VPS_USER}@${VPS_HOST}" \
  'python3.12 -c "
import sqlite3
conn = sqlite3.connect(\"/home/trader/swing-trader/trades.db\")
conn.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\")
conn.close()
print(\"  WAL checkpointed\")
"'

echo "→ Copying trades.db to local..."
scp "${VPS_USER}@${VPS_HOST}:${VPS_DB}" "${LOCAL_DB}"

echo "→ Row counts:"
python3 - "${LOCAL_DB}" << 'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for table in ("positions", "trade_log"):
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"   {table}: {count} rows")
open_pos = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
closed_pos = conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
print(f"   positions (open): {open_pos}  |  (closed): {closed_pos}")
EOF

echo "✓ Done — open DB Browser and hit File → Revert to reload."
