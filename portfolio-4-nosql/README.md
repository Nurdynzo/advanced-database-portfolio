# Portfolio 4 — NoSQL and Advanced Data Models

Portfolios 1–3 rebuilt Lodgely's facility-and-payments slice in PostgreSQL. This portfolio returns to the data model the production system actually uses, **MongoDB documents**, and adds a **Redis key-value** layer, using the same fictional data so the models can be compared directly.

| Model | Product | Used for in this portfolio |
|---|---|---|
| Document | MongoDB 8.0 (single-node replica set) | Buildings, tenancies, payments, budgets, requisitions |
| Key-value | Redis | Sessions, rate limiting, caching, live rankings |

## 1. Files and how to run

| File | Purpose |
|---|---|
| `docker-compose.yml` | MongoDB (replica set, needed for transactions) + Redis |
| `01_collections.js` | Collections, `$jsonSchema` + `$expr` validators, indexes |
| `02_seed.js` | The Portfolio 1/3 sample data, as documents |
| `03_validation_tests.js` | What the document model enforces, and what it does not (counterpart of P1's constraint tests) |
| `04_operations.js` | Reads, transactions, atomic updates, aggregation pipelines, text search, TTL, `explain()` |
| `05_concurrency_mongo.py` | Portfolio 3's race conditions, reproduced and fixed on MongoDB |
| `06_redis_keyvalue.py` | Key-value model: sessions, rate limiting, cache-aside, sorted-set ranking |
| `evidence/` | Output of every script |

```bash
docker compose up -d                  # MongoDB + Redis
npm install -g mongosh
pip install pymongo redis
./run_all.sh                          # ~30 seconds
```

## 2. Document model design

### 2.1 Collections and embedding decisions

The central question in document design is **embed or reference?** Each choice below follows from how the data is read, how it grows, and what must change atomically.

| Collection | Embedded inside | Referenced (by id) | Reasoning |
|---|---|---|---|
| `users` | — | — | Shared by organisations, buildings, tenancies and payments; updated independently. |
| `organisations` | `members[]` | `userId` | Membership is small, bounded, and always read with the organisation. |
| `buildings` | `unitCategories[]`, `units[]`, `staff[]` | `organisationId` | A building has tens to hundreds of units, all shown on one screen, and they change together. This matches the original system ("units have never had their own collection"). |
| `tenancies` | `cycles[]` (billing cycles), `tenant` snapshot, `unitLabel` snapshot | `buildingId`, `unitId`, `tenant.userId` | A lease has at most 12 monthly cycles (capped at 24 by the validator), and a tenant statement is a single read. The tenant name is copied in so lists need no join. |
| `payments` | `allocations[]` | `tenancyId`, `buildingId` | Unbounded (one per gateway charge) and written by an external system, so payments are not embedded in tenancies. `buildingId` is copied in so wallet totals need no join. |
| `payouts` | — | `buildingId` | Unbounded history. |
| `budgets` | `lines[]` with running `remaining` | `buildingId` | A reservation becomes **one atomic update on one document** (§5). |
| `requisitions` | `items[]` | `budgetId`, `budgetLineId`, `raisedBy` | Items are created and read with their requisition; the total is computed at write time. |
| `accessPasses` | — | `buildingId` | Short-lived; deleted automatically by a TTL index. |

**Rules applied:**

- **Embed** when the child is bounded, read with the parent, and changed with the parent.
- **Reference** when the child is unbounded, shared, or written by a different process.
- **Snapshot** (copy a few display fields) when a join would otherwise be needed on a hot read path, and the copied value is acceptable as "as of the time of writing" (a tenant's name on a lease).

### 2.2 Example document

```javascript
// tenancies (abridged): one read gives the lease, the tenant and every bill
{ _id: "ten_2", buildingId: "bld_palm_court", unitId: "u_pc_b1", unitLabel: "B1",
  tenant: { userId: "usr_chioma", fullName: "Chioma Okafor", email: "chioma.okafor@example.com" },
  leaseStart: ISODate("2026-03-01"), leaseEnd: ISODate("2027-03-01"),
  rent: { amount: Decimal128("200000"), cycle: "monthly" }, status: "active",
  cycles: [
    { _id: "t2_2026_03", chargeType: "rent", dueDate: ISODate("2026-03-01"),
      amountDue: Decimal128("200000"), amountPaid: Decimal128("200000") },
    { _id: "t2_2026_06", chargeType: "rent", dueDate: ISODate("2026-06-01"),
      amountDue: Decimal128("200000"), amountPaid: Decimal128("0") }, ... ] }
```

Money is stored as `Decimal128`, never as floating-point numbers.

### 2.3 Mapping from the relational model (Portfolio 1)

| Relational (P1) | Document (P4) |
|---|---|
| `unit`, `unit_category`, `facility_assignment` tables | Arrays inside `buildings` |
| `organisation_member` associative table | `members[]` inside `organisations` |
| `billing_cycle` table | `cycles[]` inside `tenancies` |
| `payment_allocation` associative table | `allocations[]` inside `payments`, plus `amountPaid` on each cycle |
| `budget_line` + `v_budget_line_usage` view (derived) | `lines[]` with **stored** `committedAmount` and `remaining` |
| `requisition_item` weak entity | `items[]` inside `requisitions` |
| Composite FK "unit's category in same building" | `$expr` validator inside one document |
| Foreign keys between tables | **None**: references are plain ids |

Portfolio 1 deliberately avoided stored totals because they can drift. Here `amountPaid` and `remaining` **are** stored. The risk is controlled in two ways: each total lives in the same document as the data it summarises (so one atomic update keeps them in step), and a validator rejects any document where `remaining ≠ expected − committed`.

## 3. Integrity: what the document model enforces (`03_validation_tests.js`)

| Test | Rule | MongoDB | PostgreSQL (P1) |
|---|---|---|---|
| V1 | Case-insensitive unique email | Rejected (unique index with collation) | Rejected |
| V2 | Second live tenancy on a unit | Rejected (partial unique index) | Rejected |
| V3 | Next tenant's lease **after** the current one ends | **Rejected (a legitimate lease is blocked)** | Accepted (range exclusion constraint) |
| V4 | Duplicate unit label in a building | Rejected (`$expr` + `$setUnion`) | Rejected (UNIQUE) |
| V5 | Unit using another building's category | Rejected (`$expr` + `$setIsSubset`) | Rejected (composite FK) |
| V6 | Second active facility manager | Rejected (`$expr` count) | Rejected (partial unique index) |
| V7–V12 | Bad dates, statuses, amounts, overpayment, negative budget | Rejected (`$jsonSchema` / `$expr`) | Rejected (CHECK) |
| V13 | Tenancy referencing a non-existent building/unit | **Accepted** | Rejected (FK) |
| V14 | Deleting a user who is still a tenant | **Accepted** | Rejected (FK) |

**Findings:**

- **Validators enforce rules *inside* a document very well.** V4–V6 needed composite keys or partial indexes in PostgreSQL, but here they are simple expressions over arrays in the same document. Embedding brings related data into the scope a validator can see.
- **Nothing enforces rules *between* documents.** V13 and V14 are accepted. In the original system, every cross-service id is a plain string for the same reason. The application, or a periodic consistency check, has to catch orphans.
- **No range-overlap constraint exists.** The partial unique index is stricter than necessary (V3), so signing the next tenant early requires a workaround, such as creating the new tenancy in a non-live status until the old one ends.

## 4. Operations (`04_operations.js`)

| # | Operation | Technique | Result |
|---|---|---|---|
| O1 | Building page; tenant statement | Single-document read; `$map`/`$sum` computes balances in the query | One round trip each; no joins |
| O2 | Confirm payment, then confirm again | **Multi-document transaction** + conditional `findOneAndUpdate({status:"pending"})` | First credits June; second is `skipped` |
| O3 | Overpaying payment | Transaction where a later write fails validation | Whole transaction aborted; the earlier status change was rolled back |
| O4 | Approve two ₦180,000 requisitions on a ₦300,000 line | One conditional `updateOne` with `$elemMatch: { remaining: { $gte: total } }` | req_3 approved, req_4 rejected; ₦120,000 remains |
| O5 | Two users edit one requisition | `version` field in the update filter | Second edit matches 0 documents; no lost update |
| O6a | Building wallet | Aggregation with `$unionWith` (payments + payouts) | Income ₦2,850,000 − withdrawn ₦1,700,000 = ₦1,150,000 |
| O6b | Overdue report | `$unwind` embedded cycles, `$match` with `$expr` | Three overdue months listed |
| O6c | Requisitions with requester name | `$lookup` (a join, used where no snapshot is stored) | Names joined from `users` |
| O7 | Search users | `$text` index | Finds the word "okafor", but **not** the fragment "kafo" |
| O8 | Visitor pass expiry | TTL index on `expiresOn` | Expired pass deleted automatically within seconds |
| O9 | Wallet filter on 100,000 payments | `explain("executionStats")` | COLLSCAN examined 100,004 documents; IXSCAN examined 200 |

Three of these are worth highlighting:

- **O2:** MongoDB supports multi-document ACID transactions on replica sets. The original system never uses them (zero across all services). O2 shows the payment-confirmation flow done safely, and O3 shows a failure halfway through rolling back cleanly, the document counterpart of Portfolio 3 Scenario 1.
- **O4:** the budget check and the reservation are one operation on one document. This is the **document-model alternative to SERIALIZABLE**: put the data a rule depends on into one document, and the single-document atomicity MongoDB always provides is enough.
- **O7:** compare with Portfolio 2 Q2. `$text` is word-based, so substring search needs a different tool (Atlas Search, or n-gram fields). This matches the audit finding that the original system falls back to an unindexable regex.

## 5. Concurrency on MongoDB (`05_concurrency_mongo.py`)

Portfolio 3's races, run with two threads against MongoDB:

| # | Scenario | Result |
|---|---|---|
| C1 | **Unsafe** payment confirmation: read status, verify (2 s), then write. This is the pattern in the original system's webhook/verify handler. | Both threads read `pending`; June **and** July credited for one ₦200,000 payment. **Double credit.** |
| C2 | Conditional `findOneAndUpdate` inside a transaction | B hit a **WriteConflict** on A's uncommitted write; the driver retried B automatically (15 attempts in the reference run; the count varies) until A committed, then B saw `succeeded` and skipped. Correct. |
| C3 | **Unsafe** budget approval: read `remaining`, check in code, then `$inc` | Both approved in code; B's write was then **rejected by the validator** (`remaining` would be −60,000). Data stayed correct, but only as an error. |
| C4 | Guard and `$inc` in one `updateOne` | A approved, B cleanly rejected (0 documents modified). Correct. |

**MongoDB vs PostgreSQL concurrency control:**

- MongoDB transactions use **optimistic concurrency at the document level**. A second writer to a document with an uncommitted change fails immediately with a `WriteConflict` (a *transient* error), and the driver's `with_transaction` retries. PostgreSQL instead makes the second writer **wait** for the row lock (Portfolio 3, Scenario 2).
- The most effective MongoDB technique is to **avoid needing a transaction**: model the data so the rule's inputs share one document, then use a conditional atomic update (C4, O4).
- C3 shows that the validator from §3 acts as a safety net, much like Portfolio 1's constraints did in Portfolio 3. The original system's budget model stores only `spentAmount`, with no invariant like `remaining ≥ 0`, so the same race overspends silently.

## 6. Key-value model with Redis (`06_redis_keyvalue.py`)

| # | Use | Redis structure | Why key-value fits |
|---|---|---|---|
| K1 | Login sessions | String (JSON) + `EX 1800`, renewed on each request | Checked on **every** API call: needs a sub-millisecond key lookup and automatic expiry |
| K2 | Login rate limit (10 per IP per minute) | `INCR` + `EXPIRE` in a `MULTI` pipeline | `INCR` is atomic across all servers, with no read-then-write race; attempts 11–12 were blocked |
| K3 | Building wallet cache | Cache-aside, `SET … EX 300`, `DEL` on change | Miss 3.4 ms → hit 0.08 ms. Shown **stale** after a payout until the key was invalidated. |
| K4 | Live ranking of buildings by collections | Sorted set, `ZINCRBY` / `ZREVRANGE` | Updated incrementally per payment, always current (contrast: P2's materialised view refresh) |

Keys follow a namespaced convention (`lodgely:session:<id>`, `lodgely:cache:wallet:<buildingId>`, …) so they can be monitored and purged by prefix. Redis is not the system of record for any of this: losing it means users log in again, limits reset and caches refill, but no money data is lost.

## 7. Suitability: which model for which part of Lodgely?

| Data | Best fit | Reason |
|---|---|---|
| Buildings, units, staff | **Document** | Read as one aggregate; flexible attributes; validators cover the internal rules |
| Tenancies with billing cycles | **Document** (with care) | One-read statements and single-document atomic updates; but no FK to units and no date-overlap constraint |
| Payments, allocations, payouts (the money ledger) | **Relational** preferred | Cross-entity rules (allocations ≤ payment, same tenancy), foreign keys and SERIALIZABLE give guarantees the document model needs application code for. MongoDB transactions make it *possible*, but the protection is opt-in per code path, as C1 shows. |
| Budgets and requisitions | **Document** | Embedding lines with `remaining` turns the budget rule into one atomic update (O4/C4) |
| Sessions, rate limits, caches, live rankings | **Key-value** | Speed and automatic expiry matter more than queries or durability |
| User search | **Search engine** (neither) | Substring and fuzzy search need trigram or n-gram indexing (pg_trgm in P2; Atlas Search for MongoDB) |

A **graph** model was considered but not used: Lodgely's relationships are shallow (organisation → building → unit → tenancy) and are always queried from a known starting point, so multi-hop traversal, the strength of a graph database, is not needed.

**Conclusion:** the document model suits Lodgely's aggregates (a building with its units, a lease with its bills) and makes many rules *simpler* than in SQL, as long as the rule's data lives in one document. It is weakest where correctness spans documents, which is exactly where the money is. That is where the original system's real risks were found: a double credit, a budget race, and fire-and-forget events.

## 8. Takeaways

1. **Design documents around access patterns and atomicity**, not around entities. Ask what is read together and what must change together.
2. **One document is the unit of atomicity.** Moving a rule's data into one document plus a conditional update (O4) beats both read-check-write and transactions.
3. **Validators are powerful within a document** (`$expr` over arrays) **but blind across documents.** Referential integrity becomes the application's job.
4. **MongoDB transactions work and are cheap to adopt**, but they are opt-in: every code path that skips them (C1) keeps the old bug.
5. **Polyglot persistence:** each model has a natural home. Relational for the ledger, document for aggregates, key-value for ephemeral high-speed data.
