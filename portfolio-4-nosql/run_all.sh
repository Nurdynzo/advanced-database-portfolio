#!/usr/bin/env bash
# Portfolio 4: rebuild the MongoDB database and regenerate every evidence file.
# Requires MongoDB (replica set) on localhost:27017 and Redis on localhost:6379,
# e.g. `docker compose up -d`, plus: npm i -g mongosh ; pip install pymongo redis
set -euo pipefail
URI=${MONGO_URI:-"mongodb://localhost:27017/?directConnection=true"}
cd "$(dirname "$0")"
mkdir -p evidence

echo "1/6 Collections, validators, indexes...";  mongosh "$URI" --quiet 01_collections.js     > evidence/01_collections.txt
echo "2/6 Seed documents...";                    mongosh "$URI" --quiet 02_seed.js            > evidence/02_seed.txt
echo "3/6 Validation tests...";                  mongosh "$URI" --quiet 03_validation_tests.js > evidence/03_validation_tests.txt
echo "4/6 Operations (~10 s)...";                mongosh "$URI" --quiet 04_operations.js      > evidence/04_operations.txt
echo "5/6 Concurrency scenarios (~15 s)..."
mongosh "$URI" --quiet 01_collections.js > /dev/null && mongosh "$URI" --quiet 02_seed.js > /dev/null
python3 05_concurrency_mongo.py --uri "$URI"    > evidence/05_concurrency_mongo.txt
echo "6/6 Redis key-value demo...";              python3 06_redis_keyvalue.py --mongo "$URI" > evidence/06_redis_keyvalue.txt
echo "Done. Evidence written to ./evidence/"
