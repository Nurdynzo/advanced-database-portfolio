#!/usr/bin/env bash
# Portfolio 2: rebuild the database and regenerate every evidence file.
# Usage:  ./run_all.sh            (uses database "lodgely"; set PG* env vars if needed)
set -euo pipefail
DB=${DB:-lodgely}
cd "$(dirname "$0")"
mkdir -p evidence

echo "1/6 Recreating Portfolio 1 schema..."
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f ../portfolio-1-database-design/01_schema.sql > /dev/null 2>&1
echo "2/6 Seeding large dataset (a few minutes)..."
psql -v ON_ERROR_STOP=1 -d "$DB" -f 01_seed_large_data.sql   > evidence/01_seed_output.txt 2>&1
echo "3/6 Baseline plans..."
psql -v ON_ERROR_STOP=1 -d "$DB" -f 02_baseline_queries.sql  > evidence/02_baseline_plans.txt 2>&1
echo "4/6 Applying optimisations..."
psql -v ON_ERROR_STOP=1 -d "$DB" -f 03_optimisations.sql     > evidence/03_optimisations.txt 2>&1
echo "5/6 Optimised plans..."
psql -v ON_ERROR_STOP=1 -d "$DB" -f 04_optimised_queries.sql > evidence/04_optimised_plans.txt 2>&1
echo "6/6 Application access patterns..."
python3 05_app_patterns.py --dsn "dbname=$DB"                  > evidence/05_app_patterns.txt
python3 05_app_patterns.py --dsn "dbname=$DB" --latency-ms 2  > evidence/05_app_patterns_2ms_latency.txt
echo "Done. Evidence written to ./evidence/"
