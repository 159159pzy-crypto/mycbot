#!/usr/bin/env bash
# Restore a MyBot PostgreSQL backup produced by scripts/backup.sh.
#
# The restore DROPS AND RECREATES application objects in the target database
# (pg_restore --clean --if-exists). Stop the application roles first so no
# writer races the restore, and re-run migrations is NOT needed: the dump
# contains the schema at backup time; run `alembic upgrade head` afterwards
# only when restoring an older dump into a newer codebase.
#
# Usage:
#   scripts/restore.sh --mode compose --file backups/mybot-....dump [--yes]
#   scripts/restore.sh --mode direct --url postgresql://user:pass@host:5432/db \
#       --file backups/mybot-....dump [--yes]
set -euo pipefail

MODE=""
FILE=""
DIRECT_URL="${MYBOT_BACKUP_URL:-}"
CONFIRMED="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --url) DIRECT_URL="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    --yes) CONFIRMED="yes"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" != "compose" && "$MODE" != "direct" ]] || [[ -z "$FILE" ]]; then
  echo "usage: $0 --mode compose|direct [--url postgresql://...] --file DUMP [--yes]" >&2
  exit 2
fi
if [[ ! -f "$FILE" ]]; then
  echo "backup file not found: $FILE" >&2
  exit 2
fi

if [[ "$CONFIRMED" != "yes" ]]; then
  echo "This will OVERWRITE the target database with $FILE."
  read -r -p "Type 'restore' to continue: " REPLY
  if [[ "$REPLY" != "restore" ]]; then
    echo "aborted"
    exit 1
  fi
fi

if [[ "$MODE" == "compose" ]]; then
  docker compose exec -T postgres sh -c \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
    < "$FILE"
else
  if [[ -z "$DIRECT_URL" ]]; then
    echo "--mode direct requires --url or MYBOT_BACKUP_URL" >&2
    exit 2
  fi
  pg_restore --dbname="$DIRECT_URL" --clean --if-exists --no-owner "$FILE"
fi

echo "restore completed from $FILE"
echo "start the application roles and check /health/ready"
