#!/usr/bin/env bash
# Back up the MyBot PostgreSQL database (custom format, suitable for pg_restore).
#
# PostgreSQL is the only backup-critical store: Redis holds transient stream
# coordination and daily counters that rebuild themselves, and SearXNG holds a
# cache. See docs/RUNBOOK.md for the rehearsed restore drill.
#
# Usage:
#   scripts/backup.sh --mode compose [--out backups/]        # via docker compose
#   scripts/backup.sh --mode direct --url postgresql://user:pass@host:5432/db \
#       [--out backups/]                                     # direct connection
set -euo pipefail

MODE=""
OUT_DIR="backups"
DIRECT_URL="${MYBOT_BACKUP_URL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --url) DIRECT_URL="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" != "compose" && "$MODE" != "direct" ]]; then
  echo "usage: $0 --mode compose|direct [--url postgresql://...] [--out DIR]" >&2
  exit 2
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"
TARGET="$OUT_DIR/mybot-$STAMP.dump"

if [[ "$MODE" == "compose" ]]; then
  # Credentials come from the running container's own environment.
  docker compose exec -T postgres sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$TARGET"
else
  if [[ -z "$DIRECT_URL" ]]; then
    echo "--mode direct requires --url or MYBOT_BACKUP_URL" >&2
    exit 2
  fi
  pg_dump --dbname="$DIRECT_URL" -Fc --file="$TARGET"
fi

SIZE="$(wc -c < "$TARGET")"
if [[ "$SIZE" -lt 1024 ]]; then
  echo "backup looks too small ($SIZE bytes) — refusing to call this a success" >&2
  exit 1
fi

echo "backup written: $TARGET ($SIZE bytes)"
echo "verify restorability regularly: see docs/RUNBOOK.md 'Backup and restore drill'"
