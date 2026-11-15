#!/usr/bin/env bash
# Portfolio 1: build the schema, load the sample data and save the constraint-test evidence.
set -euo pipefail
DB=${DB:-lodgely}
cd "$(dirname "$0")"

# --- preflight: fail with a clear message instead of a silent exit code 127 ---
command -v psql >/dev/null 2>&1 || { echo "ERROR: psql not found. Install PostgreSQL 16 and add its bin folder to PATH (see README)."; exit 1; }
psql -d "$DB" -c 'select 1' >/dev/null 2>&1 || { echo "ERROR: cannot connect to database '$DB'. Is PostgreSQL running, and did you run: createdb $DB ?"; exit 1; }
mkdir -p evidence
PGOPTIONS="-c client_min_messages=warning" psql -q -v ON_ERROR_STOP=1 -d "$DB" -f 01_schema.sql -f 02_sample_data.sql > /dev/null
psql -d "$DB" -f 03_constraint_tests.sql > evidence/constraint_tests_output.txt 2>&1
echo "Done. Evidence written to ./evidence/constraint_tests_output.txt"
