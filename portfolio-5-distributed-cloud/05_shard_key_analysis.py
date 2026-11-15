"""
Portfolio 5, step 5: choosing a shard key for the `payments` collection.

Sharding splits a collection across several replica sets (shards) by a
shard key. A poor key cannot be fixed cheaply later, so candidate keys are
evaluated here by simulation on Lodgely-shaped data (no MongoDB needed):

  - a few very large organisations and many small ones (skewed, like real life)
  - 24 months of payments, growing over time

For each candidate key the script reports:
  balance      share of all data on the fullest shard (ideal = 1 / shards)
  hotspot      share of NEW inserts (latest month) landing on the busiest shard
  targeting    shards touched by the most common query: "payments of building X"
  largest key  biggest group of documents sharing one shard-key value; it can
               never be split, so if it exceeds a chunk it becomes a "jumbo" chunk

    python 05_shard_key_analysis.py > evidence/05_shard_key_analysis.txt
"""
import bisect
import hashlib
import random
from collections import Counter, defaultdict

SHARDS = 4
CHUNKS = 64                      # chunks created for the historical data
random.seed(42)


# ---------------------------------------------------------------------------
# Synthetic, skewed Lodgely-like data
# ---------------------------------------------------------------------------
def generate():
    docs, seq = [], 0
    orgs = 200
    # Zipf-like organisation sizes: organisation 0 is huge, most are small.
    weights = [1 / (i + 1) ** 1.1 for i in range(orgs)]
    buildings = []
    for org in range(orgs):
        n_buildings = max(1, int(400 * weights[org]))
        for _ in range(n_buildings):
            buildings.append((org, f"bld_{len(buildings):05d}", random.randint(5, 60)))
    for month in range(24):
        growth = 0.4 + 0.6 * month / 23                 # platform grows over time
        for org, bld, units in buildings:
            for _ in range(int(units * growth)):
                seq += 1
                docs.append({"_id": seq,                  # stands in for a monotonic ObjectId
                             "organisationId": f"org_{org:03d}",
                             "buildingId": bld,
                             "month": month})
    return docs, len(buildings)


def h(value):
    return int(hashlib.md5(str(value).encode()).hexdigest(), 16)


# ---------------------------------------------------------------------------
# Routing for ranged and hashed keys
# ---------------------------------------------------------------------------
class RangedRouter:
    """Split the historical data into ~CHUNKS contiguous key ranges (never
    splitting a single key value) and spread chunks across shards."""

    def __init__(self, keyfn, history):
        keys = sorted(keyfn(d) for d in history)
        target = len(keys) // CHUNKS
        self.bounds = []                                  # lower bound of each chunk after the first
        count = 0
        for i, k in enumerate(keys):
            count += 1
            if count >= target and i + 1 < len(keys) and keys[i + 1] != k:
                self.bounds.append(keys[i + 1])
                count = 0
        self.keyfn = keyfn

    def shard(self, doc):
        chunk = bisect.bisect_right(self.bounds, self.keyfn(doc))
        return chunk % SHARDS                             # balancer spreads chunks evenly


class HashedRouter:
    def __init__(self, keyfn, history=None):
        self.keyfn = keyfn

    def shard(self, doc):
        return h(self.keyfn(doc)) % SHARDS


# (name, router, key function, can a "payments of building X" query include the key?)
# The application knows a building's organisation, so it can add organisationId.
CANDIDATES = [
    ("{ _id: 1 }  (ranged, monotonic)", RangedRouter, lambda d: d["_id"], False),
    ("{ _id: 'hashed' }", HashedRouter, lambda d: d["_id"], False),
    ("{ organisationId: 1 }", RangedRouter, lambda d: d["organisationId"], True),
    ("{ buildingId: 'hashed' }", HashedRouter, lambda d: d["buildingId"], True),
    ("{ buildingId: 1, _id: 1 }", RangedRouter, lambda d: (d["buildingId"], d["_id"]), True),
]


def evaluate(router_cls, keyfn, targetable, history, latest):
    router = router_cls(keyfn, history)
    alldocs = history + latest
    placement = Counter(router.shard(d) for d in alldocs)
    balance = max(placement.values()) / len(alldocs)
    inserts = Counter(router.shard(d) for d in latest)
    hotspot = max(inserts.values()) / len(latest)

    # Query targeting: "payments of building X", averaged over 50 buildings.
    by_building = defaultdict(set)
    for d in alldocs:
        by_building[d["buildingId"]].add(router.shard(d))
    sample = random.sample(sorted(by_building), 50)
    if targetable:
        targeting = sum(len(by_building[b]) for b in sample) / len(sample)
    else:
        targeting = float(SHARDS)                         # key not in the query: scatter-gather
    largest = max(Counter(keyfn(d) for d in alldocs).values()) / len(alldocs)
    return balance, hotspot, targeting, largest


def main():
    docs, n_buildings = generate()
    history = [d for d in docs if d["month"] < 23]
    latest = [d for d in docs if d["month"] == 23]
    orgs = Counter(d["organisationId"] for d in docs)
    top_org = orgs.most_common(1)[0]

    print(f"Simulated data: {len(docs):,} payments, {n_buildings:,} buildings, {len(orgs)} organisations")
    print(f"Largest organisation {top_org[0]} holds {top_org[1] / len(docs):.1%} of all payments")
    print(f"{SHARDS} shards (ideal share per shard = {1 / SHARDS:.0%}); "
          f"one chunk ~ {1 / CHUNKS:.1%} of the data\n")

    print(f"{'shard key':<34}{'balance':>9}{'hotspot':>9}{'targeting':>11}{'largest key':>13}")
    print("-" * 76)
    for name, router_cls, keyfn, targetable in CANDIDATES:
        bal, hot, tgt, big = evaluate(router_cls, keyfn, targetable, history, latest)
        jumbo = "  JUMBO" if big > 1 / CHUNKS else ""
        print(f"{name:<34}{bal:>9.1%}{hot:>9.1%}{tgt:>8.1f} sh{big:>12.2%}{jumbo}")

    print("""
Reading the table
  { _id: 1 }             Every new payment has the highest _id, so ALL new inserts go to
                         one shard (hotspot 100%) and building queries hit every shard.
  { _id: 'hashed' }      Perfect spread, but "payments of building X" must ask all shards
                         (scatter-gather) because the query does not contain the key.
  { organisationId: 1 }  Building queries hit one shard, but one organisation can never be
                         split: its data becomes a jumbo chunk and that shard fills up.
  { buildingId: 'hashed' }  Good spread and one-shard building queries; a single building
                         is small enough to never become jumbo.
  { buildingId: 1, _id: 1 } Also one-shard building queries, and a very large building
                         CAN be split (by _id) if it ever outgrows a chunk.

Recommendation: shard `payments` on { buildingId: 'hashed' } (or the compound
{ buildingId: 1, _id: 1 } if some buildings grow very large). Every payment already
carries buildingId, and the hot queries (wallet, statements, arrears) filter on it.""")


if __name__ == "__main__":
    main()
