"""
Portfolio 4, step 6: key-value model (Redis) for the parts of Lodgely that
need speed and expiry rather than rich queries.

  K1 Session store with sliding expiry
  K2 Rate limiting (fixed window counter)
  K3 Cache-aside for the building wallet balance, with invalidation
  K4 Real-time ranking with a sorted set

Usage:
    pip install redis pymongo
    python 06_redis_keyvalue.py > evidence/06_redis_keyvalue.txt
Run after 01_collections.js and 02_seed.js (K3 reads MongoDB).
"""
import argparse
import json
import secrets
import time
from decimal import Decimal

import redis
from bson.decimal128 import Decimal128
from pymongo import MongoClient

P = "lodgely:"            # key namespace prefix


def heading(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


# ---------------------------------------------------------------------------
def k1_sessions(r):
    heading("K1 Session store: key = session id, value = small JSON, expiry = 30 min")
    sid = secrets.token_hex(16)
    key = f"{P}session:{sid}"
    r.set(key, json.dumps({"userId": "usr_tunde", "roles": ["facility_manager"],
                           "buildingIds": ["bld_palm_court", "bld_cedar"]}), ex=1800)
    print(f"  SET {key[:40]}... EX 1800")
    time.sleep(1)
    print(f"  TTL after 1 s: {r.ttl(key)} s")
    session = json.loads(r.get(key))
    r.expire(key, 1800)                    # sliding expiry: each request renews the session
    print(f"  GET -> user {session['userId']}; EXPIRE renewed, TTL now {r.ttl(key)} s")
    r.delete(key)                           # logout
    print(f"  DEL (logout) -> exists? {bool(r.exists(key))}")
    print("  Why Redis: every API request validates the session, so the lookup must be a\n"
          "  sub-millisecond key read, and expiry must be automatic.")


# ---------------------------------------------------------------------------
def k2_rate_limit(r, limit=10, window=60):
    heading(f"K2 Rate limiting: at most {limit} login attempts per IP per {window} s")
    ip = "203.0.113.7"
    results = []
    for _ in range(12):
        bucket = int(time.time() // window)
        key = f"{P}ratelimit:login:{ip}:{bucket}"
        pipe = r.pipeline()                 # MULTI/EXEC: both commands run atomically
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = pipe.execute()
        results.append("allow" if count <= limit else "BLOCK")
    print("  attempts: " + " ".join(results))
    print("  INCR is atomic, so concurrent requests from many servers can never both read\n"
          "  the same count (no read-then-write race).")


# ---------------------------------------------------------------------------
def wallet_from_mongo(mdb, building):
    rows = mdb.payments.aggregate([
        {"$match": {"buildingId": building, "status": "succeeded"}},
        {"$group": {"_id": None, "income": {"$sum": "$amount"}}}])
    income = next(rows, {"income": 0})["income"]
    rows = mdb.payouts.aggregate([
        {"$match": {"buildingId": building, "status": {"$in": ["pending", "paid"]}}},
        {"$group": {"_id": None, "out": {"$sum": "$amount"}}}])
    out = next(rows, {"out": 0})["out"]
    to_dec = lambda v: v.to_decimal() if hasattr(v, "to_decimal") else Decimal(v)
    return str(to_dec(income) - to_dec(out))


def get_wallet(r, mdb, building):
    key = f"{P}cache:wallet:{building}"
    cached = r.get(key)
    if cached is not None:
        return cached, "HIT "
    value = wallet_from_mongo(mdb, building)
    r.set(key, value, ex=300)               # 5-minute safety net if an invalidation is missed
    return value, "MISS"


def k3_cache_aside(r, mdb):
    heading("K3 Cache-aside: wallet balance cached in Redis, invalidated on change")
    b = "bld_palm_court"
    r.delete(f"{P}cache:wallet:{b}")
    for i in range(3):
        t = time.perf_counter()
        value, kind = get_wallet(r, mdb, b)
        print(f"  read {i + 1}: {kind} balance = {Decimal(value):,.2f}  "
              f"({(time.perf_counter() - t) * 1000:.2f} ms)")

    print("  A payout of N100,000 is requested (MongoDB write) ...")
    mdb.payouts.insert_one({"_id": "po_k3", "reference": "PO-K3-DEMO", "buildingId": b,
                            "requestedBy": "usr_tunde", "amount": Decimal128("100000"),
                            "platformFee": Decimal128("1000"), "status": "pending"})
    value, kind = get_wallet(r, mdb, b)
    print(f"  without invalidation: {kind} balance = {Decimal(value):,.2f}   <- STALE")
    r.delete(f"{P}cache:wallet:{b}")        # the write path must invalidate the key
    value, kind = get_wallet(r, mdb, b)
    print(f"  after DEL (invalidate): {kind} balance = {Decimal(value):,.2f}")
    mdb.payouts.delete_one({"_id": "po_k3"})
    r.delete(f"{P}cache:wallet:{b}")
    print("  Trade-off: faster reads, but every write that changes a balance must invalidate\n"
          "  the key, or users see stale money figures until the TTL expires.")


# ---------------------------------------------------------------------------
def k4_ranking(r):
    heading("K4 Sorted set: live ranking of buildings by rent collected this month")
    key = f"{P}rank:collections:2026-11"
    r.delete(key)
    payments = [("bld_palm_court", 200000), ("bld_cedar", 450000), ("bld_palm_court", 1800000),
                ("bld_harbour_view", 300000), ("bld_cedar", 150000)]
    for building, amount in payments:
        r.zincrby(key, amount, building)    # O(log n) update on each confirmed payment
    r.expire(key, 60 * 60 * 24 * 40)
    for rank, (member, score) in enumerate(r.zrevrange(key, 0, 2, withscores=True), 1):
        print(f"  {rank}. {member:<18} {score:>14,.2f}")
    print("  Contrast: Portfolio 2's dashboard needed a materialised view refreshed in ~0.5 s;\n"
          "  here the ranking is updated incrementally and is always current.")
    r.delete(key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--mongo", default="mongodb://localhost:27017/?directConnection=true")
    a = ap.parse_args()
    r = redis.Redis.from_url(a.redis, decode_responses=True)
    mdb = MongoClient(a.mongo).lodgely
    k1_sessions(r)
    k2_rate_limit(r)
    k3_cache_aside(r, mdb)
    k4_ranking(r)


if __name__ == "__main__":
    main()
