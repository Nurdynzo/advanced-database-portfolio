-- =====================================================================
-- Portfolio 2, step 4: AFTER. Same six questions, with the indexes from
-- step 3 and, where needed, a rewritten query.
--   psql -d lodgely -f 04_optimised_queries.sql > evidence/04_optimised_plans.txt 2>&1
-- =====================================================================
SET search_path TO lodgely;

\echo '=================================================================='
\echo 'Q1  Building wallet balance: query unchanged, new indexes only'
\echo '=================================================================='
\o /dev/null
SELECT * FROM v_building_wallet WHERE building_id = 500;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_building_wallet WHERE building_id = 500;

\echo '=================================================================='
\echo 'Q2  User search: query unchanged, trigram GIN indexes'
\echo '=================================================================='
\o /dev/null
SELECT user_id, email, full_name FROM app_user
WHERE email ILIKE '%ade4321%' OR full_name ILIKE '%ade4321%'
ORDER BY created_at DESC LIMIT 30;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT user_id, email, full_name FROM app_user
WHERE email ILIKE '%ade4321%' OR full_name ILIKE '%ade4321%'
ORDER BY created_at DESC LIMIT 30;

\echo '=================================================================='
\echo 'Q3a Overdue report: ORIGINAL query (via the view) with new indexes'
\echo '    Shows that indexes alone do not help: the view still aggregates'
\echo '    every cycle on the platform before the building filter applies.'
\echo '=================================================================='
\o /dev/null
SELECT t.tenancy_id, u.label, au.full_name, cb.due_date, cb.balance
FROM v_cycle_balance cb
JOIN tenancy t   ON t.tenancy_id = cb.tenancy_id
JOIN unit u      ON u.unit_id = t.unit_id
JOIN app_user au ON au.user_id = t.tenant_user_id
WHERE u.building_id = 500 AND cb.balance > 0 AND cb.due_date < current_date
ORDER BY cb.due_date;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT t.tenancy_id, u.label, au.full_name, cb.due_date, cb.balance
FROM v_cycle_balance cb
JOIN tenancy t   ON t.tenancy_id = cb.tenancy_id
JOIN unit u      ON u.unit_id = t.unit_id
JOIN app_user au ON au.user_id = t.tenant_user_id
WHERE u.building_id = 500 AND cb.balance > 0 AND cb.due_date < current_date
ORDER BY cb.due_date;

\echo '=================================================================='
\echo 'Q3b Overdue report: REWRITTEN to filter by building BEFORE aggregating'
\echo '=================================================================='
\o /dev/null
SELECT t.tenancy_id, u.label, au.full_name, bc.due_date,
       bc.amount_due - COALESCE(sum(pa.amount), 0) AS balance
FROM unit u
JOIN tenancy t        ON t.unit_id = u.unit_id
JOIN app_user au      ON au.user_id = t.tenant_user_id
JOIN billing_cycle bc ON bc.tenancy_id = t.tenancy_id
LEFT JOIN payment_allocation pa ON pa.cycle_id = bc.cycle_id
WHERE u.building_id = 500 AND bc.due_date < current_date
GROUP BY t.tenancy_id, u.label, au.full_name, bc.cycle_id
HAVING bc.amount_due - COALESCE(sum(pa.amount), 0) > 0
ORDER BY bc.due_date;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT t.tenancy_id, u.label, au.full_name, bc.due_date,
       bc.amount_due - COALESCE(sum(pa.amount), 0) AS balance
FROM unit u
JOIN tenancy t        ON t.unit_id = u.unit_id
JOIN app_user au      ON au.user_id = t.tenant_user_id
JOIN billing_cycle bc ON bc.tenancy_id = t.tenancy_id
LEFT JOIN payment_allocation pa ON pa.cycle_id = bc.cycle_id
WHERE u.building_id = 500 AND bc.due_date < current_date
GROUP BY t.tenancy_id, u.label, au.full_name, bc.cycle_id
HAVING bc.amount_due - COALESCE(sum(pa.amount), 0) > 0
ORDER BY bc.due_date;

\echo '=================================================================='
\echo 'Q4a Payment history page 25,001: OFFSET, now with a matching index'
\echo '=================================================================='
\o /dev/null
SELECT payment_id, payment_reference, amount, status, initiated_at
FROM payment ORDER BY initiated_at DESC, payment_id DESC
OFFSET 500000 LIMIT 20;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT payment_id, payment_reference, amount, status, initiated_at
FROM payment ORDER BY initiated_at DESC, payment_id DESC
OFFSET 500000 LIMIT 20;

\echo '=================================================================='
\echo 'Q4b Same page with KEYSET pagination. In an application the cursor'
\echo '    is the last row of the previous page; here it is looked up once.'
\echo '=================================================================='
SELECT initiated_at AS cursor_ts, payment_id AS cursor_id
FROM payment ORDER BY initiated_at DESC, payment_id DESC
OFFSET 499999 LIMIT 1 \gset
\echo 'cursor =' :'cursor_ts' :cursor_id
EXPLAIN (ANALYZE, BUFFERS)
SELECT payment_id, payment_reference, amount, status, initiated_at
FROM payment
WHERE (initiated_at, payment_id) < (:'cursor_ts'::timestamptz, :cursor_id)
ORDER BY initiated_at DESC, payment_id DESC
LIMIT 20;

\echo '=================================================================='
\echo 'Q5  Reconciliation: query unchanged, partial index on pending rows'
\echo '=================================================================='
\o /dev/null
SELECT payment_id, payment_reference, tenancy_id, amount, initiated_at
FROM payment
WHERE status = 'pending' AND initiated_at < now() - interval '1 hour'
ORDER BY initiated_at LIMIT 100;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT payment_id, payment_reference, tenancy_id, amount, initiated_at
FROM payment
WHERE status = 'pending' AND initiated_at < now() - interval '1 hour'
ORDER BY initiated_at LIMIT 100;

\echo '=================================================================='
\echo 'Q6  Dashboard: read from the materialised view'
\echo '=================================================================='
EXPLAIN (ANALYZE, BUFFERS)
SELECT building_id, cycles_in_arrears, total_arrears, refreshed_at
FROM mv_building_arrears
ORDER BY total_arrears DESC LIMIT 20;

\echo '=================================================================='
\echo 'W1  Write cost: insert 50,000 payments (rolled back), now with more indexes'
\echo '=================================================================='
\timing on
BEGIN;
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, initiated_at)
SELECT 1 + (g % 60000), 'WRITE-TEST-' || g, 150000, 'card', 'pending', now()
FROM generate_series(1, 50000) g;
ROLLBACK;
\timing off
