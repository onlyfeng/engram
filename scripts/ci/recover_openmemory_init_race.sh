#!/usr/bin/env bash
# Recover from OpenMemory's startup pg_type race.
#
# Why: upstream OpenMemory's first-run init occasionally fails with
#   duplicate key value violates unique constraint "pg_type_typname_nsp_index"
# Postgres creates a row type for every table, so the conflicting "type" is
# almost always a leftover table from a previous half-initialised run. We drop
# everything in the openmemory schema (idempotent, name-agnostic) and let the
# migrator re-create on next start.
#
# Usage:
#   recover_openmemory_init_race.sh              # exec via docker compose
#   recover_openmemory_init_race.sh --print-sql  # emit SQL only (no DB needed)
#
# Env:
#   POSTGRES_DB     defaults to "engram"
#   POSTGRES_USER   defaults to "postgres"
#   COMPOSE_FILE    defaults to "docker-compose.unified.yml"
#   COMPOSE_SERVICE defaults to "postgres"

set -euo pipefail

POSTGRES_DB="${POSTGRES_DB:-engram}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.unified.yml}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-postgres}"

print_sql() {
  cat <<'SQL'
-- Drop every table in the openmemory schema; CASCADE removes the row types
-- that caused the pg_type duplicate-key error during startup.
DO $$
DECLARE
  r RECORD;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'openmemory') THEN
    RAISE NOTICE 'openmemory schema absent; nothing to recover';
    RETURN;
  END IF;
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'openmemory'
  LOOP
    EXECUTE format('DROP TABLE IF EXISTS openmemory.%I CASCADE', r.tablename);
  END LOOP;
  -- Real custom types (enum/composite/domain) — anything still hanging around
  -- after table drops is owned by the migrator and safe to remove.
  FOR r IN
    SELECT t.typname
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'openmemory'
      AND t.typtype IN ('e', 'c', 'd')
      AND NOT EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.reltype = t.oid
      )
  LOOP
    EXECUTE format('DROP TYPE IF EXISTS openmemory.%I CASCADE', r.typname);
  END LOOP;
END $$;
SQL
}

if [[ "${1:-}" == "--print-sql" ]]; then
  print_sql
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[recover_openmemory_init_race] docker not available" >&2
  exit 2
fi

print_sql | docker compose -f "$COMPOSE_FILE" exec -T "$COMPOSE_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f -
