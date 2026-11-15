-- =====================================================================
-- Portfolio 3, Scenario 1: atomicity (single session).
--   psql -d lodgely -f 02_atomicity.sql > evidence/02_atomicity.txt 2>&1
-- Recording a payment touches two tables (payment + payment_allocation).
-- Both changes must happen together or not at all.
-- =====================================================================
SET search_path TO lodgely;
\set ON_ERROR_STOP off

\echo '--- Starting state: tenancy 1 has no outstanding balance; 0 payments with ref DEMO-%'
SELECT count(*) AS demo_payments FROM payment WHERE payment_reference LIKE 'DEMO-%';

\echo ''
\echo '=== 1a. COMMIT: payment + allocation saved together ==='
-- Give tenancy 1 a new cycle to pay.
INSERT INTO billing_cycle (tenancy_id, charge_type, period, amount_due, due_date)
VALUES (1, 'service_charge', '[2027-01-01,2028-01-01)', 250000, '2027-01-01');

BEGIN;
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, confirmed_at)
VALUES (1, 'DEMO-COMMIT', 250000, 'card', 'succeeded', now());
SELECT allocate_payment(payment_id) AS unallocated
FROM payment WHERE payment_reference = 'DEMO-COMMIT';
COMMIT;

SELECT p.payment_reference, p.amount, pa.cycle_id, pa.amount AS allocated
FROM payment p JOIN payment_allocation pa USING (payment_id)
WHERE p.payment_reference = 'DEMO-COMMIT';

\echo ''
\echo '=== 1b. Explicit ROLLBACK: application cancels after a failed business check ==='
BEGIN;
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, confirmed_at)
VALUES (1, 'DEMO-ROLLBACK', 99000, 'card', 'succeeded', now());
\echo 'Inside the transaction, the row exists:'
SELECT payment_reference, amount FROM payment WHERE payment_reference = 'DEMO-ROLLBACK';
ROLLBACK;
\echo 'After ROLLBACK, it is gone:'
SELECT count(*) AS rows_after_rollback FROM payment WHERE payment_reference = 'DEMO-ROLLBACK';

\echo ''
\echo '=== 1c. Error inside a transaction: the whole transaction is aborted ==='
BEGIN;
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, confirmed_at)
VALUES (2, 'DEMO-ERROR', 200000, 'card', 'succeeded', now());
\echo 'Allocate tenancy 2''s payment to tenancy 1''s cycle -> FK violation:'
INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount)
SELECT payment_id, 1, 2, 200000 FROM payment WHERE payment_reference = 'DEMO-ERROR';
\echo 'Any further statement is refused until the transaction ends:'
SELECT 1;
COMMIT;   -- PostgreSQL turns this COMMIT into a ROLLBACK
\echo 'The payment inserted BEFORE the error was not saved either (no half-recorded payment):'
SELECT count(*) AS rows_after_error FROM payment WHERE payment_reference = 'DEMO-ERROR';

\echo ''
\echo '=== 1d. SAVEPOINT: undo only the failed step, keep the rest ==='
BEGIN;
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, confirmed_at)
VALUES (2, 'DEMO-SAVEPOINT', 100000, 'card', 'succeeded', now());
SAVEPOINT before_allocation;
\echo 'First attempt: wrong cycle (belongs to another tenancy) -> error'
INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount)
SELECT payment_id, 1, 2, 100000 FROM payment WHERE payment_reference = 'DEMO-SAVEPOINT';
ROLLBACK TO SAVEPOINT before_allocation;
\echo 'Second attempt: correct arrears-first allocation'
SELECT allocate_payment(payment_id) AS unallocated
FROM payment WHERE payment_reference = 'DEMO-SAVEPOINT';
COMMIT;

SELECT p.payment_reference, pa.cycle_id, pa.amount AS allocated
FROM payment p JOIN payment_allocation pa USING (payment_id)
WHERE p.payment_reference = 'DEMO-SAVEPOINT';
\echo 'Arrears-first: the oldest unpaid cycle (June, cycle 6) received the N100,000:'
SELECT cycle_id, period, amount_due, amount_paid, balance FROM v_cycle_balance WHERE cycle_id = 6;
