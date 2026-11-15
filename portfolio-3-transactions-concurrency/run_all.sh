#!/usr/bin/env bash
# Portfolio 3: rebuild the small dataset and regenerate every evidence file.
set -euo pipefail
DB=${DB:-lodgely}
cd "$(dirname "$0")"
mkdir -p evidence
P1=../portfolio-1-database-design

reset() {
  psql -q -v ON_ERROR_STOP=1 -d "$DB" -f $P1/01_schema.sql -f $P1/02_sample_data.sql -f 01_setup.sql > /dev/null 2>&1
}

echo "1/2 Scenario 1: atomicity (COMMIT / ROLLBACK / SAVEPOINT)..."
reset
psql -d "$DB" -f 02_atomicity.sql > evidence/02_atomicity.txt 2>&1
echo "2/2 Scenarios 2-6: concurrency (takes ~40 seconds)..."
python3 03_concurrency_scenarios.py --dsn "dbname=$DB" > evidence/03_concurrency.txt 2>&1
echo "Done. Evidence written to ./evidence/"
