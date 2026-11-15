-- =====================================================================
-- Portfolio 2, step 2: BASELINE. Six realistic queries run with only the
-- Portfolio 1 schema (primary keys, unique keys, FK indexes).
--   psql -d lodgely -f 02_baseline_queries.sql > evidence/02_baseline_plans.txt 2>&1
--
-- Each query runs once silently (to warm the cache, so before/after runs
-- are compared fairly) and then under EXPLAIN (ANALYZE, BUFFERS).
-- =====================================================================
SET search_path TO lodgely;

\echo '=================================================================='
\echo 'Q1  Building wallet balance (view v_building_wallet), building 500'
\echo '=================================================================='
\o /dev/null
SELECT * FROM v_building_wallet WHERE building_id = 500;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_building_wallet WHERE building_id = 500;

\echo '=================================================================='
\echo 'Q2  Admin user search: substring match on email or name'
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
\echo 'Q3  Overdue report for one building, written against v_cycle_balance'
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
\echo 'Q4  Payment history, page 25,001 (OFFSET 500000 LIMIT 20)'
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
\echo 'Q5  Reconciliation: oldest 100 payments still pending after 1 hour'
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
\echo 'Q6  Platform dashboard: top 20 buildings by total arrears'
\echo '=================================================================='
\o /dev/null
SELECT u.building_id, count(*) AS cycles_in_arrears, sum(cb.balance) AS total_arrears
FROM v_cycle_balance cb
JOIN tenancy t ON t.tenancy_id = cb.tenancy_id
JOIN unit u    ON u.unit_id = t.unit_id
WHERE cb.balance > 0 AND cb.due_date < current_date
GROUP BY u.building_id
ORDER BY total_arrears DESC LIMIT 20;
\o
EXPLAIN (ANALYZE, BUFFERS)
SELECT u.building_id, count(*) AS cycles_in_arrears, sum(cb.balance) AS total_arrears
FROM v_cycle_balance cb
JOIN tenancy t ON t.tenancy_id = cb.tenancy_id
JOIN unit u    ON u.unit_id = t.unit_id
WHERE cb.balance > 0 AND cb.due_date < current_date
GROUP BY u.building_id
ORDER BY total_arrears DESC LIMIT 20;

\echo '=================================================================='
\echo 'W1  Write cost: insert 50,000 payments (rolled back)'
\echo '=================================================================='
\timing on
BEGIN;
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, initiated_at)
SELECT 1 + (g % 60000), 'WRITE-TEST-' || g, 150000, 'card', 'pending', now()
FROM generate_series(1, 50000) g;
ROLLBACK;
\timing off
