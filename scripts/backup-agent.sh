#!/usr/bin/env sh
set -eu

OUTPUT=${1:-"backups/mybot-agent-$(date -u +%Y%m%dT%H%M%SZ).json"}
REMOTE=${2:-}

uv run mybot export --output "$OUTPUT" --include-history

if [ -n "$REMOTE" ]; then
  if ! command -v rclone >/dev/null 2>&1; then
    echo "rclone is required when a remote destination is supplied" >&2
    exit 2
  fi
  rclone copyto "$OUTPUT" "${REMOTE%/}/$(basename "$OUTPUT")"
fi

printf '%s\n' "$OUTPUT"
