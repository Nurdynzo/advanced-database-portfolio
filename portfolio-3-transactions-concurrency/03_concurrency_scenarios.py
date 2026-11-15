"""
Portfolio 3: automated concurrency scenarios.

Each scenario opens two real database sessions (A and B), runs them
concurrently with controlled timing, prints a timestamped log of what each
session did, and then checks the final state of the data. The database is
rebuilt before every scenario so results never depend on a previous run.

Usage:
    pip install "psycopg[binary]"
    python 03_concurrency_scenarios.py                 # all scenarios
    python 03_concurrency_scenarios.py --only 3        # one scenario
    python 03_concurrency_scenarios.py > evidence/03_concurrency.txt

Connection: standard PG* environment variables plus --dsn (default "dbname=lodgely").
"""
import argparse
import pathlib
import threading
import time

import psycopg
from psycopg import errors

HERE = pathlib.Path(__file__).resolve().parent
P1 = HERE.parent / "portfolio-1-database-design"
SETUP_FILES = [P1 / "01_schema.sql", P1 / "02_sample_data.sql", HERE / "01_setup.sql"]

DSN = "dbname=lodgely"
T0 = time.perf_counter()
PRINT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(who, msg):
    with PRINT_LOCK:
        print(f"  [+{time.perf_counter() - T0:5.2f}s] {who}: {msg}", flush=True)


def heading(title):
    global T0
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78, flush=True)
    T0 = time.perf_counter()


def reset_database():
    with psycopg.connect(DSN, autocommit=True) as conn:
        for f in SETUP_FILES:
            conn.execute(f.read_text())


class Session:
    """One database connection; transactions are controlled explicitly."""

    def __init__(self, name):
        self.name = name
        self.conn = psycopg.connect(DSN, autocommit=True,
                                    options="-c search_path=lodgely")

    def run(self, sql, params=None, show=True):
        if show:
            log(self.name, " ".join(sql.split()))
        cur = self.conn.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount

    def close(self):
        self.conn.close()


def query(sql, params=None):
    with psycopg.connect(DSN, options="-c search_path=lodgely") as conn:
        return conn.execute(sql, params).fetchall()


def run_concurrently(task_a, task_b, b_delay=0.5):
    """Start task_a, start task_b b_delay seconds later, wait for both."""
    ta = threading.Thread(target=task_a)
    tb = threading.Thread(target=task_b)
    ta.start()
    time.sleep(b_delay)
    tb.start()
    ta.join()
    tb.join()


# ---------------------------------------------------------------------------
# Scenario 2: duplicate payment confirmation (webhook + manual verify)
# ---------------------------------------------------------------------------

def scenario_double_credit():
    ref = "PSK-0004-SAMPLE"   # N200,000 pending payment for tenancy 2
    variants = [
        ("2a UNSAFE check-then-act (READ COMMITTED)", "confirm_payment_naive"),
        ("2b FIX A: atomic conditional UPDATE",      "confirm_payment_atomic"),
        ("2c FIX B: SELECT ... FOR UPDATE",          "confirm_payment_locked"),
    ]
    for title, fn in variants:
        heading(f"Scenario {title}\n"
                f"A = gateway webhook, B = manual 'verify' click, same payment {ref}")
        reset_database()

        def confirm(name):
            s = Session(name)
            try:
                s.run("BEGIN")
                (result,), = s.run(f"SELECT {fn}(%s, 2)", (ref,))
                log(name, f"-> {result}")
                s.run("COMMIT")
            except psycopg.Error as e:
                log(name, f"ERROR {e.sqlstate}: {e.diag.message_primary} -> rolled back")
                s.run("ROLLBACK")
            finally:
                s.close()

        run_concurrently(lambda: confirm("A"), lambda: confirm("B"))

        (amount, allocated), = query(
            """SELECT p.amount, COALESCE(SUM(pa.amount), 0)
               FROM payment p LEFT JOIN payment_allocation pa USING (payment_id)
               WHERE p.payment_reference = %s GROUP BY p.amount""", (ref,))
        verdict = "CORRECT" if allocated == amount else "WRONG: tenant credited twice"
        print(f"  RESULT: payment amount = {amount:,.2f}; "
              f"total credited to rent = {allocated:,.2f}  -> {verdict}")


# ---------------------------------------------------------------------------
# Scenario 3: budget overspend (write skew) and isolation levels
# ---------------------------------------------------------------------------

def scenario_budget_write_skew():
    variants = [
        ("3a READ COMMITTED",                 "READ COMMITTED",  "approve_requisition"),
        ("3b REPEATABLE READ",                "REPEATABLE READ", "approve_requisition"),
        ("3c SERIALIZABLE (with retry)",      "SERIALIZABLE",    "approve_requisition"),
        ("3d READ COMMITTED + lock budget line", "READ COMMITTED", "approve_requisition_locked"),
    ]
    for title, level, fn in variants:
        heading(f"Scenario {title}\n"
                "Budget line 'Plumbing repairs' = N300,000. "
                "A approves requisition 3 (N180,000), B approves requisition 4 (N180,000)")
        reset_database()

        def approve(name, req_id):
            s = Session(name)
            try:
                for attempt in (1, 2):
                    try:
                        s.run(f"BEGIN ISOLATION LEVEL {level}")
                        (result,), = s.run(f"SELECT {fn}(%s, 2)", (req_id,))
                        log(name, f"-> {result}")
                        s.run("COMMIT")
                        log(name, "COMMIT succeeded")
                        return
                    except errors.SerializationFailure as e:
                        log(name, f"ERROR {e.sqlstate}: {e.diag.message_primary}")
                        s.run("ROLLBACK")
                        if attempt == 1:
                            log(name, "retrying the whole transaction...")
            finally:
                s.close()

        run_concurrently(lambda: approve("A", 3), lambda: approve("B", 4))

        (expected, committed), = query(
            "SELECT expected_amount, committed_amount FROM v_budget_line_usage "
            "WHERE budget_line_id = 2")
        verdict = "CORRECT" if committed <= expected else "WRONG: budget overspent"
        print(f"  RESULT: budget = {expected:,.2f}; approved = {committed:,.2f}  -> {verdict}")


# ---------------------------------------------------------------------------
# Scenario 4: lost update (read-modify-write from the application)
# ---------------------------------------------------------------------------

ITEM = "requisition_id = 1 AND line_no = 1"   # 'Engine oil (litres)', quantity 20


def scenario_lost_update():
    # 4a: naive
    heading("Scenario 4a UNSAFE read-modify-write (lost update)\n"
            "A adds 5 litres, B removes 2 litres, both edit what they READ earlier")
    reset_database()
    a, b = Session("A"), Session("B")
    qa = a.run(f"SELECT quantity FROM requisition_item WHERE {ITEM}")[0]
    qb = b.run(f"SELECT quantity FROM requisition_item WHERE {ITEM}")[0]
    log("A", f"read quantity = {qa[0]}, user sets it to {qa[0] + 5}")
    a.run(f"UPDATE requisition_item SET quantity = %s WHERE {ITEM}", (qa[0] + 5,))
    log("B", f"read quantity = {qb[0]} (stale), user sets it to {qb[0] - 2}")
    b.run(f"UPDATE requisition_item SET quantity = %s WHERE {ITEM}", (qb[0] - 2,))
    final = query(f"SELECT quantity FROM lodgely.requisition_item WHERE {ITEM}")[0]
    print(f"  RESULT: final quantity = {final[0]} (expected 23)  -> "
          f"{'CORRECT' if final[0] == 23 else 'WRONG: A’s change was silently lost'}")
    a.close(); b.close()

    # 4b: optimistic locking
    heading("Scenario 4b FIX: optimistic concurrency control (version column)")
    reset_database()
    a, b = Session("A"), Session("B")
    ra = a.run(f"SELECT quantity, version FROM requisition_item WHERE {ITEM}")[0]
    rb = b.run(f"SELECT quantity, version FROM requisition_item WHERE {ITEM}")[0]
    n = a.run(f"UPDATE requisition_item SET quantity = %s, version = version + 1 "
              f"WHERE {ITEM} AND version = %s", (ra[0] + 5, ra[1]))
    log("A", f"rows updated = {n}")
    n = b.run(f"UPDATE requisition_item SET quantity = %s, version = version + 1 "
              f"WHERE {ITEM} AND version = %s", (rb[0] - 2, rb[1]))
    log("B", f"rows updated = {n}  -> conflict detected, reload and re-apply")
    rb = b.run(f"SELECT quantity, version FROM requisition_item WHERE {ITEM}")[0]
    n = b.run(f"UPDATE requisition_item SET quantity = %s, version = version + 1 "
              f"WHERE {ITEM} AND version = %s", (rb[0] - 2, rb[1]))
    log("B", f"rows updated = {n}")
    final = query(f"SELECT quantity, version FROM lodgely.requisition_item WHERE {ITEM}")[0]
    print(f"  RESULT: final quantity = {final[0]}, version = {final[1]}  -> "
          f"{'CORRECT' if final[0] == 23 else 'WRONG'}")
    a.close(); b.close()

    # 4c: REPEATABLE READ detects it
    heading("Scenario 4c FIX: REPEATABLE READ detects the concurrent update")
    reset_database()
    a, b = Session("A"), Session("B")
    b.run("BEGIN ISOLATION LEVEL REPEATABLE READ")
    qb = b.run(f"SELECT quantity FROM requisition_item WHERE {ITEM}")[0]
    qa = a.run(f"SELECT quantity FROM requisition_item WHERE {ITEM}")[0]
    a.run(f"UPDATE requisition_item SET quantity = %s WHERE {ITEM}", (qa[0] + 5,))
    log("A", "committed (autocommit)")
    try:
        b.run(f"UPDATE requisition_item SET quantity = %s WHERE {ITEM}", (qb[0] - 2,))
        b.run("COMMIT")
    except errors.SerializationFailure as e:
        log("B", f"ERROR {e.sqlstate}: {e.diag.message_primary}")
        b.run("ROLLBACK")
        log("B", "application retries: re-reads and re-applies -2")
        b.run(f"UPDATE requisition_item SET quantity = quantity - 2 WHERE {ITEM}")
    final = query(f"SELECT quantity FROM lodgely.requisition_item WHERE {ITEM}")[0]
    print(f"  RESULT: final quantity = {final[0]}  -> {'CORRECT' if final[0] == 23 else 'WRONG'}")
    a.close(); b.close()


# ---------------------------------------------------------------------------
# Scenario 5: deadlock and lock ordering
# ---------------------------------------------------------------------------

def scenario_deadlock():
    heading("Scenario 5a DEADLOCK: two jobs lock the same payouts in opposite order\n"
            "A = 'mark paid' batch (payout 1 then 2), B = finance cancels (payout 2 then 1)")
    reset_database()
    a, b = Session("A"), Session("B")
    a.run("BEGIN"); a.run("UPDATE payout_request SET status = 'paid' WHERE payout_id = 1")
    b.run("BEGIN"); b.run("UPDATE payout_request SET status = 'cancelled' WHERE payout_id = 2")

    def step(s, sql):
        try:
            s.run(sql)
            s.run("COMMIT")
            log(s.name, "COMMIT succeeded")
        except errors.DeadlockDetected as e:
            log(s.name, f"ERROR {e.sqlstate}: {e.diag.message_primary} -> rolled back")
            s.run("ROLLBACK")

    run_concurrently(
        lambda: step(a, "UPDATE payout_request SET status = 'paid' WHERE payout_id = 2"),
        lambda: step(b, "UPDATE payout_request SET status = 'cancelled' WHERE payout_id = 1"),
        b_delay=0.3)
    rows = query("SELECT payout_id, status FROM lodgely.payout_request ORDER BY payout_id")
    print(f"  RESULT: {rows}  -> PostgreSQL detected the cycle and aborted one transaction")
    a.close(); b.close()

    heading("Scenario 5b FIX: both jobs lock rows in the same order (ORDER BY id FOR UPDATE)")
    reset_database()
    a, b = Session("A"), Session("B")

    def job(s, status):
        s.run("BEGIN")
        s.run("SELECT payout_id FROM payout_request WHERE payout_id IN (1, 2) "
              "ORDER BY payout_id FOR UPDATE")
        log(s.name, "holds both locks")
        time.sleep(1)
        s.run("UPDATE payout_request SET status = %s WHERE payout_id IN (1, 2) "
              "AND status = 'pending'", (status,))
        s.run("COMMIT")
        log(s.name, "COMMIT succeeded")

    run_concurrently(lambda: job(a, "paid"), lambda: job(b, "cancelled"), b_delay=0.3)
    rows = query("SELECT payout_id, status FROM lodgely.payout_request ORDER BY payout_id")
    print(f"  RESULT: {rows}  -> B waited instead of deadlocking; "
          "its `status = 'pending'` guard then matched nothing")
    a.close(); b.close()


# ---------------------------------------------------------------------------
# Scenario 6: read phenomena under different isolation levels
# ---------------------------------------------------------------------------

BALANCE_SQL = "SELECT SUM(balance) FROM v_cycle_balance WHERE tenancy_id = 2"


def scenario_read_phenomena():
    heading("Scenario 6a No dirty reads: even READ UNCOMMITTED never sees uncommitted data")
    reset_database()
    a, b = Session("A"), Session("B")
    b.run("BEGIN")
    b.run("UPDATE tenancy SET rent_amount = 999999 WHERE tenancy_id = 2")
    a.run("BEGIN ISOLATION LEVEL READ UNCOMMITTED")
    r = a.run("SELECT rent_amount FROM tenancy WHERE tenancy_id = 2")[0]
    log("A", f"sees rent_amount = {r[0]:,.2f} (B's uncommitted 999,999 is invisible)")
    a.run("COMMIT"); b.run("ROLLBACK")
    a.close(); b.close()

    for level in ("READ COMMITTED", "REPEATABLE READ"):
        heading(f"Scenario 6{'b' if level == 'READ COMMITTED' else 'c'} "
                f"Tenant statement under {level}: balance read twice while a payment commits")
        reset_database()
        a, b = Session("A"), Session("B")
        a.run(f"BEGIN ISOLATION LEVEL {level}")
        r1 = a.run(BALANCE_SQL)[0]
        log("A", f"first read: outstanding balance = {r1[0]:,.2f}")
        b.run("BEGIN")
        b.run("INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, "
              "confirmed_at) VALUES (2, 'DEMO-NRR', 200000, 'card', 'succeeded', now())")
        b.run("SELECT allocate_payment(payment_id) FROM payment "
              "WHERE payment_reference = 'DEMO-NRR'")
        b.run("COMMIT")
        r2 = a.run(BALANCE_SQL)[0]
        log("A", f"second read: outstanding balance = {r2[0]:,.2f}")
        a.run("COMMIT")
        same = r1[0] == r2[0]
        print(f"  RESULT: {'same value both times (consistent snapshot)' if same else 'NON-REPEATABLE READ: value changed inside one transaction'}")
        a.close(); b.close()


SCENARIOS = {
    "2": scenario_double_credit,
    "3": scenario_budget_write_skew,
    "4": scenario_lost_update,
    "5": scenario_deadlock,
    "6": scenario_read_phenomena,
}


def main():
    global DSN
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DSN)
    parser.add_argument("--only", choices=SCENARIOS.keys())
    args = parser.parse_args()
    DSN = args.dsn
    for key, fn in SCENARIOS.items():
        if args.only in (None, key):
            fn()
    print("\nAll scenarios finished. The database was left in the state of the last scenario.")


if __name__ == "__main__":
    main()
