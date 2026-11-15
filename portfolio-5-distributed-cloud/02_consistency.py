"""
Portfolio 5, step 2: consistency in a replicated database.

  C1  Cost of durability: latency of w:1 vs w:"majority" vs w:3
  C2  A lagging secondary: one secondary is frozen (fsyncLock) to simulate a
      slow or partitioned node, then:
        - w:1 write succeeds; reading from the lagging node returns STALE data
        - w:"majority" still succeeds (2 of 3 is enough)
        - w:3 times out, but the write is NOT undone on the primary
        - after recovery the lagging node catches up from the oplog

    python 02_consistency.py > evidence/02_consistency.txt
"""
import statistics
import time
from datetime import datetime, timezone

from bson.decimal128 import Decimal128
from pymongo import WriteConcern
from pymongo.errors import WTimeoutError

from common import (DB_NAME, args, cluster_client, heading, log, node_client,
                    primary_and_secondaries)


def payment(ref):
    return {"reference": ref, "tenancyId": "ten_2", "buildingId": "bld_palm_court",
            "amount": Decimal128("200000"), "status": "succeeded",
            "confirmedAt": datetime.now(timezone.utc)}


def visible_on(host, ref):
    with node_client(host) as c:
        return c[DB_NAME].payments.find_one({"reference": ref}) is not None


def main():
    a = args(__doc__)
    client = cluster_client(a)
    db = client[DB_NAME]
    db.payments.drop()
    primary, secondaries = primary_and_secondaries(client)
    healthy, lagging = secondaries[0], secondaries[1]

    # ------------------------------------------------------------------
    heading("C1 Write latency by write concern (200 inserts each, median)")
    for label, wc in [("w:1        (primary only)", WriteConcern(w=1)),
                      ("w:majority (2 of 3 nodes)", WriteConcern(w="majority")),
                      ("w:3        (all nodes)", WriteConcern(w=3))]:
        coll = db.get_collection("payments", write_concern=wc)
        times = []
        for i in range(200):
            t = time.perf_counter()
            coll.insert_one(payment(f"LAT-{label[:3]}-{i}"))
            times.append((time.perf_counter() - t) * 1000)
        print(f"  {label}: median {statistics.median(times):6.2f} ms   "
              f"p95 {sorted(times)[189]:6.2f} ms")
    print("  Stronger write concerns wait for more acknowledgements. With all nodes on one\n"
          "  machine the extra wait is negligible; across availability zones or regions each\n"
          "  acknowledgement adds a network round trip, so the gap becomes milliseconds to\n"
          "  tens of milliseconds per write.")

    # ------------------------------------------------------------------
    heading(f"C2 Lagging secondary: {lagging} is frozen with fsyncLock\n"
            f"   primary = {primary}, healthy secondary = {healthy}")
    lag_admin = node_client(lagging).admin
    lag_admin.command("fsync", lock=True)
    log(f"{lagging} frozen: it can serve reads but cannot apply new writes")
    try:
        ref = "PSK-LAG-001"
        db.get_collection("payments", write_concern=WriteConcern(w=1)).insert_one(payment(ref))
        log("w:1 insert acknowledged by the primary")
        time.sleep(1)
        for host in (primary, healthy, lagging):
            log(f"read from {host:<16} -> {'found' if visible_on(host, ref) else 'NOT FOUND (stale read)'}")

        t = time.perf_counter()
        db.get_collection("payments", write_concern=WriteConcern(w="majority", wtimeout=5000)) \
          .insert_one(payment("PSK-LAG-002"))
        log(f"w:majority insert acknowledged in {(time.perf_counter() - t) * 1000:.1f} ms "
            "(primary + healthy secondary = 2 of 3)")

        t = time.perf_counter()
        try:
            db.get_collection("payments", write_concern=WriteConcern(w=3, wtimeout=3000)) \
              .insert_one(payment("PSK-LAG-003"))
            log("w:3 acknowledged")
        except WTimeoutError:
            log(f"w:3 FAILED after {(time.perf_counter() - t):.1f} s: write concern timeout")
            found = visible_on(primary, "PSK-LAG-003")
            log(f"...but the document is on the primary anyway: {'found' if found else 'not found'}"
                " -> a timeout is NOT a rollback; the client must not blindly retry")
    finally:
        lag_admin.command("fsyncUnlock")
        log(f"{lagging} unfrozen; catching up from the oplog...")

    t = time.perf_counter()
    while not all(visible_on(lagging, r) for r in ("PSK-LAG-001", "PSK-LAG-002", "PSK-LAG-003")):
        time.sleep(0.05)
    log(f"{lagging} has all 3 writes after {(time.perf_counter() - t) * 1000:.0f} ms "
        "-> eventual consistency")


if __name__ == "__main__":
    main()
