# Portfolio 3 — Transactions and Concurrency

This portfolio shows how transactions keep Lodgely's payment and budgeting data correct when operations fail halfway or run at the same time. Each concurrency problem is first **reproduced** with two real database sessions, then **fixed**, and the fix is verified by checking the final data.

The scenarios mirror risks found in the original (MongoDB-based) system, re-implemented on the PostgreSQL schema from Portfolio 1:

- a payment confirmed twice by a webhook and a manual "verify" arriving together,
- a budget check followed by a separate update,
- line items edited by read-modify-write,
- event processing that assumes nothing else changes the data meanwhile.

## 1. Files and how to run

| File | Purpose |
|---|---|
| `01_setup.sql` | Extra demo rows, a `version` column, and the stored functions (unsafe and fixed versions) |
| `02_atomicity.sql` | Scenario 1: COMMIT, ROLLBACK, error abort, SAVEPOINT (one session) |
| `03_concurrency_scenarios.py` | Scenarios 2–6: two concurrent sessions with a timestamped log and a final correctness check |
| `run_all.sh` | Rebuilds the data and regenerates all evidence |
| `evidence/` | `02_atomicity.txt`, `03_concurrency.txt`, screenshots |

```bash
pip install "psycopg[binary]"
./run_all.sh        # ~45 seconds; the database is rebuilt before every scenario
```

The scenarios use the **small** Portfolio 1 dataset, not the large Portfolio 2 one, so every amount can be checked by hand.

## 2. Results summary

| # | Scenario | Unsafe result | Fixed result | Mechanism |
|---|---|---|---|---|
| 1 | Payment + allocation failing halfway | — | No half-recorded payment | Atomic transaction, ROLLBACK, SAVEPOINT |
| 2 | Same payment confirmed twice concurrently | ₦400,000 credited for a ₦200,000 payment | ₦200,000 | Atomic conditional UPDATE, or `SELECT … FOR UPDATE` |
| 3 | Two requisitions approved against one budget | ₦360,000 approved on a ₦300,000 budget (READ COMMITTED **and** REPEATABLE READ) | ₦180,000 | SERIALIZABLE + retry, or locking the budget line |
| 4 | Two users edit one line item | Final quantity 18 (one edit lost) | 23 | Optimistic locking (version column), or REPEATABLE READ |
| 5 | Two jobs lock payouts in opposite order | Deadlock, one job aborted | Both complete | Consistent lock ordering |
| 6 | Balance read twice while a payment commits | Value changes within one transaction (READ COMMITTED) | Stable snapshot | REPEATABLE READ |

Full timestamped logs are in `evidence/03_concurrency.txt`.

## 3. Scenario 1 — Atomicity

Recording a payment writes to two tables, `payment` and `payment_allocation`. If only the first write succeeded, a payment would show as `succeeded` but no rent would be credited.

| Case | What happens (`evidence/02_atomicity.txt`) |
|---|---|
| 1a COMMIT | Payment and allocation become visible together. |
| 1b ROLLBACK | The inserted row is visible inside the transaction, then gone after `ROLLBACK`. |
| 1c Error | The allocation violates the same-tenancy foreign key. PostgreSQL then refuses every further statement (`current transaction is aborted`) and turns the final `COMMIT` into a `ROLLBACK`. The payment inserted **before** the error is also gone. |
| 1d SAVEPOINT | After the failed allocation, `ROLLBACK TO SAVEPOINT` undoes only that step. The payment is kept, a correct arrears-first allocation is made, and everything commits. |

The original system performs this kind of two-document update without a transaction, so a crash between the two writes leaves them inconsistent. Here the database guarantees all-or-nothing.

## 4. Scenario 2 — Duplicate payment confirmation (lost idempotency)

**Situation:** the payment gateway's webhook and a tenant clicking "verify payment" arrive at the same moment for the same pending ₦200,000 payment.

**Unsafe code (`confirm_payment_naive`):** read the status; if it is not `succeeded`, verify with the gateway (2 s), then set `succeeded` and allocate. This is a *check-then-act* pattern.

```
[+0.09s] A: SELECT confirm_payment_naive('PSK-0004-SAMPLE', 2)
[+0.59s] B: SELECT confirm_payment_naive('PSK-0004-SAMPLE', 2)   <- B also reads 'pending'
[+2.09s] A: -> credited ; COMMIT
[+2.59s] B: -> credited ; COMMIT
RESULT: payment amount = 200,000.00; total credited to rent = 400,000.00 -> WRONG
```

Both sessions read `pending` before either wrote anything. B's `UPDATE` waited for A's row lock and then went ahead, because it did not re-check the status. B's allocation ran after A committed, so B saw June as already paid and credited July instead. One ₦200,000 payment settled two months of rent.

The unique `payment_reference` does not prevent this: it stops the same payment from being *inserted* twice, not *processed* twice.

> **Observation from testing:** with different data, where both sessions tried to credit the same cycle, B failed with a primary-key violation on `payment_allocation (payment_id, cycle_id)`. The schema can turn some double credits into errors, but which case occurs depends on the tenant's balances, so the key is a safety net, not a fix.

**Fix A, atomic conditional update (`confirm_payment_atomic`):**

```sql
UPDATE payment SET status = 'succeeded', confirmed_at = now()
WHERE payment_reference = $1 AND status = 'pending'
RETURNING payment_id;
```

The check and the write are one statement. Under READ COMMITTED, B's `UPDATE` blocks on A's row lock. When A commits, PostgreSQL **re-evaluates B's `WHERE` clause against the new committed row**, finds `status = 'succeeded'`, and updates nothing. B sees zero rows and stops.

**Fix B, pessimistic lock (`confirm_payment_locked`):** `SELECT … FOR UPDATE` locks the row *before* the check, so B waits until A commits and then reads `succeeded`.

| | Fix A (conditional update) | Fix B (`FOR UPDATE`) |
|---|---|---|
| Lock held during gateway call | No | Yes, so other requests for this payment wait ~2 s |
| Gateway called twice | Yes (harmless, read-only) | No |
| Best when | The slow step is safe to repeat | The slow step must happen only once |

Fix A is preferred here: verifying with the gateway is safe to repeat, and holding locks across network calls hurts throughput.

## 5. Scenario 3 — Budget overspend (write skew) and isolation levels

**Situation:** the "Plumbing repairs" budget line is ₦300,000. Two managers simultaneously approve different requisitions of ₦180,000 each. `approve_requisition` checks *committed + requested ≤ budget*, then approves.

| Variant | A | B | Approved total | Correct? |
|---|---|---|---:|---|
| 3a READ COMMITTED | approved | approved | ₦360,000 | No |
| 3b REPEATABLE READ | approved | approved | ₦360,000 | **No** |
| 3c SERIALIZABLE | approved | `40001` serialization failure → retry → rejected | ₦180,000 | Yes |
| 3d READ COMMITTED + `FOR UPDATE` on budget line | approved | waits → rejected | ₦180,000 | Yes |

**Why REPEATABLE READ is not enough:** this anomaly is **write skew**. A and B read the same data (the budget line's committed total) but write *different rows* (requisition 3 and requisition 4). REPEATABLE READ only detects two transactions updating the *same* row, so neither is aborted, and each committed decision rests on a premise the other invalidated.

**SERIALIZABLE (Serializable Snapshot Isolation):** PostgreSQL tracks what each transaction read. It sees that A wrote a row B had read, and B wrote a row A had read. No serial order of A and B could produce both approvals, so it aborts B at commit with SQLSTATE `40001`. The application must **retry** the whole transaction. On retry, B sees A's approval and correctly rejects.

**Locking the budget line ("materialising the conflict"):** both transactions lock the same `budget_line` row first, so the second waits and then reads the updated total. This works at READ COMMITTED, but only if *every* code path that commits budget remembers to take the lock.

| | SERIALIZABLE + retry | Lock the budget line |
|---|---|---|
| Protects code paths that forget | Yes, the database detects conflicts | No |
| Cost | Retries; occasional false-positive aborts | Approvals on one line run one at a time |
| Application must | Retry on `40001` | Always lock first |

## 6. Scenario 4 — Lost update (application read-modify-write)

**Situation:** two staff open the same requisition line (20 litres of engine oil). A adds 5, B removes 2. The correct result is 23.

| Variant | Result | How |
|---|---|---|
| 4a Unsafe | **18** | Both read 20; A writes 25; B writes 18 from its stale read. A's change silently disappears. |
| 4b Optimistic locking | 23 | `UPDATE … SET quantity = ?, version = version + 1 WHERE … AND version = ?`. B's update matches **0 rows** because the version moved, so B reloads (25, v2) and re-applies. |
| 4c REPEATABLE READ | 23 | B's update fails with `40001 could not serialize access due to concurrent update`; B retries. |

Optimistic locking suits screens where a user may look at a record for minutes before saving, because no lock is held while they think. The same lost-update risk exists in the original system's invoice "mark paid" flow, which loads a document, changes one line item in memory and saves the whole document back. A third option, when the change is arithmetic, is to let the database do it: `SET quantity = quantity + 5`.

## 7. Scenario 5 — Deadlock

**Situation:** a "mark payouts paid" batch job (A) updates payout 1 then payout 2, while finance (B) cancels payout 2 then payout 1.

- **5a:** A holds payout 1 and waits for payout 2; B holds payout 2 and waits for payout 1. After `deadlock_timeout` (1 s), PostgreSQL detects the cycle and aborts one transaction with `40P01 deadlock detected`. The other completes.
- **5b (fix):** both jobs first run `SELECT … WHERE payout_id IN (1,2) ORDER BY payout_id FOR UPDATE`. Locks are always taken in the same order, so a cycle is impossible: B simply waits. Its `AND status = 'pending'` guard then matches nothing, so B cannot cancel payouts A has already paid.

## 8. Scenario 6 — Read phenomena

| Variant | Observation |
|---|---|
| 6a READ UNCOMMITTED | A does **not** see B's uncommitted change. PostgreSQL never allows dirty reads; READ UNCOMMITTED behaves as READ COMMITTED. |
| 6b READ COMMITTED | A tenant statement reads the outstanding balance, a payment commits, and a second read **in the same transaction** returns a different value (non-repeatable read). The statement's total and its line items could disagree. |
| 6c REPEATABLE READ | Both reads return the same value: the transaction works on one snapshot taken at its first query. |

Multi-query reports such as statements, reconciliations and exports should run at REPEATABLE READ (or `READ ONLY` snapshots) so all their numbers describe the same moment.

## 9. Isolation levels in PostgreSQL (as observed)

| Level | Dirty read | Non-repeatable read | Lost update (same row) | Write skew |
|---|---|---|---|---|
| READ UNCOMMITTED* | Prevented (6a) | Possible | Possible | Possible |
| READ COMMITTED (default) | Prevented | Possible (6b) | Possible (4a) | Possible (3a) |
| REPEATABLE READ | Prevented | Prevented (6c) | Prevented (4c) | **Possible (3b)** |
| SERIALIZABLE | Prevented | Prevented | Prevented | Prevented (3c) |

\* Treated as READ COMMITTED by PostgreSQL.

## 10. Manual two-terminal demonstrations (for screenshots)

Open two `psql` sessions side by side (`psql -d lodgely`, then `SET search_path TO lodgely;` in each). Reset the data before each demo:

```bash
psql -d lodgely -f ../portfolio-1-database-design/01_schema.sql \
                -f ../portfolio-1-database-design/02_sample_data.sql -f 01_setup.sql
```

The functions take a delay argument; use 10 seconds so there is time to switch terminals.

**Double credit (Scenario 2a):**

| Step | Terminal A | Terminal B |
|---|---|---|
| 1 | `BEGIN;` | |
| 2 | `SELECT confirm_payment_naive('PSK-0004-SAMPLE', 10);` | |
| 3 | | `BEGIN;` then, within 10 s, `SELECT confirm_payment_naive('PSK-0004-SAMPLE', 10);` |
| 4 | Returns `credited`. Run `COMMIT;` | A few seconds later, B also returns `credited` |
| 5 | | `COMMIT;` |
| 6 | `SELECT * FROM payment_allocation WHERE payment_id = 4;` shows 2 rows (June and July) totalling ₦400,000 | |

Repeat with `confirm_payment_atomic`: B returns `skipped: already processed by another request`.

**Write skew (Scenario 3):**

| Step | Terminal A | Terminal B |
|---|---|---|
| 1 | `BEGIN ISOLATION LEVEL SERIALIZABLE;` | `BEGIN ISOLATION LEVEL SERIALIZABLE;` |
| 2 | `SELECT approve_requisition(3, 10);` | (within 10 s) `SELECT approve_requisition(4, 10);` |
| 3 | `COMMIT;` succeeds | |
| 4 | | `COMMIT;` → `ERROR: could not serialize access…` |

Repeat with `REPEATABLE READ`: both commits succeed and `SELECT * FROM v_budget_line_usage WHERE budget_line_id = 2;` shows ₦360,000 against ₦300,000.

**Deadlock (Scenario 5a):**

| Step | Terminal A | Terminal B |
|---|---|---|
| 1 | `BEGIN; UPDATE payout_request SET status='paid' WHERE payout_id=1;` | |
| 2 | | `BEGIN; UPDATE payout_request SET status='cancelled' WHERE payout_id=2;` |
| 3 | `UPDATE payout_request SET status='paid' WHERE payout_id=2;` (waits) | |
| 4 | | `UPDATE payout_request SET status='cancelled' WHERE payout_id=1;` → `ERROR: deadlock detected` |

## 11. Takeaways

1. **Check-then-act is the root of most concurrency bugs** (Scenarios 2, 3, 4). The safest fixes make the check part of the write (conditional `UPDATE`), lock what the check depends on, or let SERIALIZABLE detect the conflict.
2. **Isolation level names are not guarantees of correctness.** REPEATABLE READ still allowed the budget overspend. Only SERIALIZABLE prevents every anomaly tested, at the cost of retries.
3. **SERIALIZABLE requires retry logic.** A `40001` error is expected behaviour, not a bug.
4. **Locks must be taken in a consistent order** to avoid deadlocks, and should not be held across slow external calls.
5. **Constraints from Portfolio 1 are a safety net, not a substitute** for correct transactions. The allocation primary key caught some double credits but not all.
