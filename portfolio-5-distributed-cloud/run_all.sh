#!/usr/bin/env bash
# Portfolio 5: run every demonstration and save the evidence.
# Prerequisites: `docker compose up -d` (wait ~20 s for the election),
# hosts file entry "127.0.0.1 mongo1 mongo2 mongo3", and `pip install pymongo`.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p evidence
echo "1/5 Cluster status...";            python3 01_cluster_status.py       > evidence/01_cluster_status.txt
echo "2/5 Consistency (~10 s)...";       python3 02_consistency.py          > evidence/02_consistency.txt
echo "3/5 Failover (~40 s)...";          python3 03_failover.py             > evidence/03_failover.txt
echo "4/5 Outbox + change stream...";    python3 04_outbox_change_stream.py > evidence/04_outbox.txt
echo "5/5 Shard key analysis...";        python3 05_shard_key_analysis.py   > evidence/05_shard_key_analysis.txt
python3 01_cluster_status.py > evidence/01_cluster_status_after_failover.txt
echo "Done. Evidence written to ./evidence/"
