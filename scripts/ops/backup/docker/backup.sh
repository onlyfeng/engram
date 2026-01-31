#!/usr/bin/env bash
set -euo pipefail

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-postgres}"
PGUSER="${PGUSER:-postgres}"
PGDB="${PGDB:-engram_project}"
OUTDIR="${OUTDIR:-./_backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"

mkdir -p "$OUTDIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUTFILE="$OUTDIR/engram_${PGDB}_${TS}.dump"

echo "Backing up to: $OUTFILE"
docker exec -e PGPASSWORD="${PGPASSWORD:-}" "$POSTGRES_CONTAINER" \
  pg_dump -U "$PGUSER" -F c "$PGDB" > "$OUTFILE"

find "$OUTDIR" -name "engram_${PGDB}_*.dump" -mtime +"$KEEP_DAYS" -delete
echo "✅ Backup done."
