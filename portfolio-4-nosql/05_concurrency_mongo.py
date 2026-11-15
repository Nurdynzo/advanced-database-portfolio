"""
Portfolio 4, step 5: the Portfolio 3 concurrency scenarios, on MongoDB.

Two threads (A and B) act on the same documents at the same time, exactly
as in Portfolio 3, but using the document model and MongoDB's own tools:
atomic single-document updates, conditional filters, validators and
multi-document transactions.

Usage:
    pip install pymongo
    python 05_concurrency_mongo.py > evidence/05_concurrency_mongo.txt
    python 05_concurrency_mongo.py --uri "mongodb://localhost:27017/?directConnection=true"
Run after 01_collections.js and 02_seed.js.
"""
import argparse
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from bson.decimal128 import Decimal128
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import OperationFailure, WriteError

T0 = time.perf_counter()
LOCK = threading.Lock()
REF = "PSK-0004-SAMPLE"


def log(who, msg):
    with LOCK:
        print(f"  [+{time.perf_counter() - T0:5.2f}s] {who}: {msg}", flush=True)


def heading(title):
    global T0
    print("\n" + "=" * 74 + f"\n{title}\n" + "=" * 74, flush=True)
    T0 = time.perf_counter()


def dec(x):
    return x.to_decimal() if isinstance(x, Decimal128) else Decimal(str(x))


def d128(x):
    return Decimal128(str(x))


def both(task_a, task_b, delay=0.5):
    ta, tb = threading.Thread(target=task_a), threading.Thread(target=task_b)
    ta.start(); time.sleep(delay); tb.start(); ta.join(); tb.join()


def reset(db):
    """Put the documents these scenarios touch back to their seed state."""
    db.payments.update_one({"reference": REF},
                           {"$set": {"status": "pending", "allocations": []},
                            "$unset": {"confirmedAt": ""}})
    db.tenancies.update_one({"_id": "ten_2"},
                            {"$set": {"cycles.$[c].amountPaid": d128(0)}},
                            array_filters=[{"c._id": {"$in": ["t2_2026_06", "t2_2026_07",
                                                              "t2_2026_08", "t2_2026_09"]}}])
    db.budgets.update_one({"_id": "bud_pc_2026", "lines._id": "bl_plumbing"},
                          {"$set": {"lines.$.committedAmount": d128(0),
                                    "lines.$.remaining": d128(300000)}})
    db.requisitions.update_many({"_id": {"$in": ["req_3", "req_4"]}},
                                {"$set": {"status": "submitted"}})


def allocate(db, payment, session=None):
    """Arrears-first allocation of a payment onto the tenancy's embedded cycles."""
    ten = db.tenancies.find_one({"_id": payment["tenancyId"]}, session=session)
    remaining, allocations = dec(payment["amount"]), []
    for c in sorted(ten["cycles"], key=lambda c: c["dueDate"]):
        owed = dec(c["amountDue"]) - dec(c["amountPaid"])
        if remaining <= 0 or owed <= 0:
            continue
        part = min(remaining, owed)
        db.tenancies.update_one({"_id": ten["_id"], "cycles._id": c["_id"]},
                                {"$inc": {"cycles.$.amountPaid": d128(part)}}, session=session)
        allocations.append({"cycleId": c["_id"], "amount": d128(part)})
        remaining -= part
    db.payments.update_one({"_id": payment["_id"]},
                           {"$push": {"allocations": {"$each": allocations}}}, session=session)
    return ", ".join(f"{a['cycleId']}={a['amount']}" for a in allocations)


def credited(db):
    ten = db.tenancies.find_one({"_id": "ten_2"})
    paid = sum(dec(c["amountPaid"]) for c in ten["cycles"]
               if c["_id"] in ("t2_2026_06", "t2_2026_07", "t2_2026_08", "t2_2026_09"))
    return paid


# ---------------------------------------------------------------------------
# C1 / C2: duplicate payment confirmation
# ---------------------------------------------------------------------------

def c1_naive(client):
    db = client.lodgely
    heading("C1 UNSAFE: read status, verify with gateway, then write (no transaction)\n"
            "This is the pattern found in the original system's webhook/verify handler")
    reset(db)

    def confirm(name):
        p = db.payments.find_one({"reference": REF})
        log(name, f"read status = {p['status']}")
        if p["status"] == "succeeded":
            log(name, "-> skipped"); return
        time.sleep(2)                                            # gateway verification
        db.payments.update_one({"_id": p["_id"]},
                               {"$set": {"status": "succeeded",
                                         "confirmedAt": datetime.now(timezone.utc)}})
        log(name, "-> credited " + allocate(db, p))

    both(lambda: confirm("A"), lambda: confirm("B"))
    total = credited(db)
    print(f"  RESULT: payment = 200,000.00; credited = {total:,.2f} -> "
          f"{'CORRECT' if total == 200000 else 'WRONG: tenant credited twice'}")


def c2_transaction(client):
    db = client.lodgely
    heading("C2 FIX: conditional findOneAndUpdate inside a multi-document transaction\n"
            "(allocation made slow on purpose so B collides with A's UNCOMMITTED write)")
    reset(db)

    def confirm(name):
        attempts = 0

        def body(session):
            nonlocal attempts
            attempts += 1
            p = db.payments.find_one_and_update(
                {"reference": REF, "status": "pending"},          # check + claim in one step
                {"$set": {"status": "succeeded", "confirmedAt": datetime.now(timezone.utc)}},
                return_document=ReturnDocument.AFTER, session=session)
            if p is None:
                return "skipped: already processed"
            log(name, f"claimed the payment (attempt {attempts}); allocating...")
            time.sleep(2)                                         # slow work inside the transaction
            return "credited " + allocate(db, p, session)

        with client.start_session() as s:
            result = s.with_transaction(body)
        log(name, f"-> {result}  (transaction attempts: {attempts})")

    both(lambda: confirm("A"), lambda: confirm("B"))
    total = credited(db)
    print(f"  RESULT: payment = 200,000.00; credited = {total:,.2f} -> "
          f"{'CORRECT' if total == 200000 else 'WRONG'}")
    print("  B's attempts hit a WriteConflict (TransientTransactionError) while A's transaction was\n"
          "  open; the driver retried B automatically until A committed, then B saw 'succeeded'.")


# ---------------------------------------------------------------------------
# C3 / C4: budget reservation
# ---------------------------------------------------------------------------

def budget_line(db):
    b = db.budgets.find_one({"_id": "bud_pc_2026"})
    return next(l for l in b["lines"] if l["_id"] == "bl_plumbing")


def c3_naive_budget(client):
    db = client.lodgely
    heading("C3 UNSAFE: read remaining budget, check in code, then $inc\n"
            "Budget N300,000; A approves req_3 (N180,000), B approves req_4 (N180,000)")
    reset(db)

    def approve(name, req_id):
        req = db.requisitions.find_one({"_id": req_id})
        line = budget_line(db)
        log(name, f"read remaining = {dec(line['remaining']):,.2f}; requested {dec(req['total']):,.2f}")
        if dec(line["remaining"]) < dec(req["total"]):
            log(name, "-> rejected"); return
        time.sleep(2)
        try:
            db.budgets.update_one({"_id": "bud_pc_2026", "lines._id": "bl_plumbing"},
                                  {"$inc": {"lines.$.remaining": d128(-dec(req["total"])),
                                            "lines.$.committedAmount": req["total"]}})
            db.requisitions.update_one({"_id": req_id}, {"$set": {"status": "approved"}})
            log(name, "-> approved")
        except WriteError as e:
            log(name, f"-> WRITE FAILED: error {e.code} document failed validation "
                      "(remaining would go below 0)")

    both(lambda: approve("A", "req_3"), lambda: approve("B", "req_4"))
    line = budget_line(db)
    print(f"  RESULT: committed = {dec(line['committedAmount']):,.2f} of 300,000.00. "
          "The validator (remaining >= 0) stopped the overspend, but only as an error\n"
          "          AFTER B's code had already decided to approve: B's user sees a failure, not a clean rejection.")


def c4_atomic_budget(client):
    db = client.lodgely
    heading("C4 FIX: guard and update in ONE operation ($elemMatch remaining >= total)")
    reset(db)

    def approve(name, req_id):
        req = db.requisitions.find_one({"_id": req_id})
        time.sleep(2)
        r = db.budgets.update_one(
            {"_id": "bud_pc_2026",
             "lines": {"$elemMatch": {"_id": "bl_plumbing", "remaining": {"$gte": req["total"]}}}},
            {"$inc": {"lines.$.remaining": d128(-dec(req["total"])),
                      "lines.$.committedAmount": req["total"]}})
        status = "approved" if r.modified_count == 1 else "rejected"
        db.requisitions.update_one({"_id": req_id}, {"$set": {"status": status}})
        log(name, f"-> {status} (budget documents modified: {r.modified_count})")

    both(lambda: approve("A", "req_3"), lambda: approve("B", "req_4"))
    line = budget_line(db)
    print(f"  RESULT: committed = {dec(line['committedAmount']):,.2f} of 300,000.00 -> CORRECT, "
          "and B got a clean business rejection")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="mongodb://localhost:27017/?directConnection=true")
    args = ap.parse_args()
    client = MongoClient(args.uri)
    for fn in (c1_naive, c2_transaction, c3_naive_budget, c4_atomic_budget):
        fn(client)
    reset(client.lodgely)
    print("\n(documents reset to seed state)")


if __name__ == "__main__":
    main()
