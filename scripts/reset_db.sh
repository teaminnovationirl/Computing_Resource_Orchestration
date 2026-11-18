#!/usr/bin/env bash
# Drop and rebuild the development database. Never run it during a measured run.
set -euo pipefail
ADMIN_URL=${ADMIN_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/postgres}
DB=${DB_NAME:-orch}
DATA=${DATA_DIR:-./data}
python -m scripts.capacity --check-database "$DB"   # D-46: stop first unless DATABASE_URL names $DB
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f db/roles.sql
psql "$ADMIN_URL" -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" -c "CREATE DATABASE $DB"
psql "${ADMIN_URL%/*}/$DB" -v ON_ERROR_STOP=1 -f db/schema.sql -f db/seed.sql
mkdir -p "$DATA/problems" "$DATA/payloads" "$DATA/runs" "$DATA/logs"
cp -r seed_data/problems/. "$DATA/problems/"
python -m scripts.capacity --profile "${CAPACITY_PROFILE:-s0}"
