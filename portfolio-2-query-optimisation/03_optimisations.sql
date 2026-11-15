-- =====================================================================
-- Portfolio 2, step 3: OPTIMISATIONS. Each index targets a specific query
-- from 02_baseline_queries.sql; the comment says which and why.
--   psql -d lodgely -f 03_optimisations.sql > evidence/03_optimisations.txt 2>&1
-- =====================================================================
SET search_path TO lodgely;
\timing on

-- Q1, Q3, Q6: tenancy's only index on unit_id was the GiST index behind the
-- exclusion constraint, which the planner avoids for plain equality lookups
-- (it chose a sequential scan of all 60,000 tenancies). A B-tree fixes that.
CREATE INDEX ix_tenancy_unit ON tenancy (unit_id);

-- Q1: partial + covering index. Holds only succeeded payments (the only rows
-- the wallet sums) and stores `amount` in the index (INCLUDE), so the sum is
-- answered by an index-only scan without visiting the table.
CREATE INDEX ix_payment_tenancy_succeeded
    ON payment (tenancy_id) INCLUDE (amount)
    WHERE status = 'succeeded';

-- Q2: B-tree indexes cannot serve '%term%' (no fixed prefix). Trigram GIN
-- indexes split text into 3-character pieces and can.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_user_email_trgm ON app_user USING gin (email gin_trgm_ops);
CREATE INDEX ix_user_name_trgm  ON app_user USING gin (full_name gin_trgm_ops);

-- Q3: lets the rewritten query fetch one tenancy's past-due cycles directly.
CREATE INDEX ix_cycle_tenancy_due ON billing_cycle (tenancy_id, due_date);

-- Q4: matches the ORDER BY exactly, removing the 500k-row sort. (The
-- remaining OFFSET cost is fixed by keyset pagination in step 4.)
CREATE INDEX ix_payment_initiated ON payment (initiated_at DESC, payment_id DESC);

-- Q5: partial index containing only pending payments (~0.2% of rows),
-- already ordered by initiated_at.
CREATE INDEX ix_payment_pending ON payment (initiated_at) WHERE status = 'pending';

-- Q6: the dashboard aggregates every billing cycle on the platform each time
-- it loads. A materialised view stores the result; the trade-off is that it
-- is only as fresh as its last REFRESH.
CREATE MATERIALIZED VIEW mv_building_arrears AS
SELECT u.building_id,
       count(*)          AS cycles_in_arrears,
       sum(cb.balance)   AS total_arrears,
       now()             AS refreshed_at
FROM v_cycle_balance cb
JOIN tenancy t ON t.tenancy_id = cb.tenancy_id
JOIN unit u    ON u.unit_id = t.unit_id
WHERE cb.balance > 0 AND cb.due_date < current_date
GROUP BY u.building_id;

-- A unique index is required for REFRESH ... CONCURRENTLY (readers are not blocked).
CREATE UNIQUE INDEX ux_mv_building_arrears ON mv_building_arrears (building_id);
CREATE INDEX ix_mv_building_arrears_total ON mv_building_arrears (total_arrears DESC);

\echo '--- Cost of keeping the materialised view fresh:'
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_building_arrears;

VACUUM ANALYZE;
\timing off

\echo '--- Storage cost of the new indexes'
SELECT indexrelname AS index_name,
       relname      AS table_name,
       pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE schemaname = 'lodgely'
  AND indexrelname IN ('ix_tenancy_unit', 'ix_payment_tenancy_succeeded',
                       'ix_user_email_trgm', 'ix_user_name_trgm',
                       'ix_cycle_tenancy_due', 'ix_payment_initiated',
                       'ix_payment_pending')
ORDER BY pg_relation_size(indexrelid) DESC;

SELECT pg_size_pretty(pg_relation_size('payment')) AS payment_table_size,
       pg_size_pretty(pg_indexes_size('payment'))  AS payment_all_indexes_size;
