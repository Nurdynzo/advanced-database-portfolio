"""
Portfolio 2, step 5: application-level access patterns.

Some performance problems are not visible in a single EXPLAIN plan because
they come from HOW the application talks to the database. This script
measures two such patterns found in the original system:

  A. N+1 queries: list a building's tenancies, then look up each tenant
     separately, versus one JOIN.
  B. Fetch-all-then-sum: load every succeeded payment into the application
     and add them up, versus letting the database run SUM().

Usage:
    pip install "psycopg[binary]"
    python 05_app_patterns.py                       # local socket
    python 05_app_patterns.py --latency-ms 5        # simulate a network/service hop
    python 05_app_patterns.py > evidence/05_app_patterns.txt

Connection settings come from the standard PG* environment variables
(PGHOST, PGUSER, PGPASSWORD, ...) plus --dsn (default "dbname=lodgely").
"""
import argparse
import statistics
import time

import psycopg

BUILDING_ID = 500
REPEATS = 5


def timed(fn, repeats=REPEATS):
    """Run fn several times; return (median_ms, result_of_last_run)."""
    times, result = [], None
    for _ in range(repeats):
        start = time.perf_counter()
        result = fn()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), result


class Db:
    """Thin wrapper that counts round trips and can add artificial latency."""

    def __init__(self, conn, latency_ms):
        self.conn = conn
        self.latency = latency_ms / 1000
        self.round_trips = 0

    def query(self, sql, params=None):
        self.round_trips += 1
        if self.latency:
            time.sleep(self.latency)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


# ---------- Pattern A: N+1 vs JOIN ----------

def tenancy_list_n_plus_one(db):
    tenancies = db.query(
        """SELECT t.tenancy_id, u.label, t.tenant_user_id, t.status
           FROM tenancy t JOIN unit u ON u.unit_id = t.unit_id
           WHERE u.building_id = %s""",
        (BUILDING_ID,),
    )
    rows = []
    for tenancy_id, label, tenant_id, status in tenancies:   # one query per row
        (name, email), = db.query(
            "SELECT full_name, email FROM app_user WHERE user_id = %s", (tenant_id,)
        )
        rows.append((tenancy_id, label, status, name, email))
    return rows


def tenancy_list_join(db):
    return db.query(
        """SELECT t.tenancy_id, u.label, t.status, au.full_name, au.email
           FROM tenancy t
           JOIN unit u      ON u.unit_id = t.unit_id
           JOIN app_user au ON au.user_id = t.tenant_user_id
           WHERE u.building_id = %s""",
        (BUILDING_ID,),
    )


# ---------- Pattern B: fetch-all-then-sum vs SUM() ----------

def income_fetch_all(db, building_filter):
    sql = """SELECT p.amount FROM payment p
             JOIN tenancy t ON t.tenancy_id = p.tenancy_id
             JOIN unit u    ON u.unit_id = t.unit_id
             WHERE p.status = 'succeeded'""" + building_filter
    rows = db.query(sql)
    return sum(r[0] for r in rows), len(rows)


def income_sql_sum(db, building_filter):
    sql = """SELECT COALESCE(SUM(p.amount), 0) FROM payment p
             JOIN tenancy t ON t.tenancy_id = p.tenancy_id
             JOIN unit u    ON u.unit_id = t.unit_id
             WHERE p.status = 'succeeded'""" + building_filter
    (total,), = db.query(sql)
    return total, 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default="dbname=lodgely")
    parser.add_argument("--latency-ms", type=float, default=0.0,
                        help="artificial delay per round trip (simulates a network hop)")
    args = parser.parse_args()

    with psycopg.connect(args.dsn, options="-c search_path=lodgely") as conn:
        db = Db(conn, args.latency_ms)
        print(f"Simulated latency per round trip: {args.latency_ms} ms; "
              f"median of {REPEATS} runs\n")

        print("A. Tenancy list for one building (with tenant name and email)")
        for label, fn in [("N+1 queries", tenancy_list_n_plus_one), ("single JOIN", tenancy_list_join)]:
            db.round_trips = 0
            ms, rows = timed(lambda: fn(db))
            print(f"   {label:<14} {ms:9.2f} ms   rows={len(rows):>5}   "
                  f"round trips per call={db.round_trips // REPEATS}")

        print("\nB. Total succeeded income")
        scopes = [("one building", f" AND u.building_id = {BUILDING_ID}"),
                  ("whole platform", "")]
        for scope, flt in scopes:
            for label, fn in [("fetch all + sum in app", income_fetch_all),
                              ("SUM() in database", income_sql_sum)]:
                ms, (total, rows_moved) = timed(lambda: fn(db, flt))
                print(f"   {scope:<15} {label:<24} {ms:9.2f} ms   "
                      f"rows sent to app={rows_moved:>7}   total={total:,.2f}")


if __name__ == "__main__":
    main()
