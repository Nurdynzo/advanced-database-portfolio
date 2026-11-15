-- =====================================================================
-- Portfolio 1: constraint tests. Every INSERT/UPDATE marked "EXPECT ERROR"
-- should be rejected by the database. Save the output as evidence:
--   psql -d lodgely -f 03_constraint_tests.sql > evidence/constraint_tests_output.txt 2>&1
-- =====================================================================
SET search_path TO lodgely;
\set ON_ERROR_STOP off

\echo '--- T1 EXPECT ERROR: overlapping tenancy on unit A1 (exclusion constraint)'
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle, status)
VALUES (1, 5, '[2026-06-01,2027-06-01)', 1800000, 'annual', 'pending');

\echo '--- T2 EXPECT SUCCESS: back-to-back lease on A1 starting the day the current one ends'
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle, status)
VALUES (1, 5, '[2027-01-01,2028-01-01)', 1900000, 'annual', 'pending');

\echo '--- T3 EXPECT SUCCESS: overlap allowed when the older tenancy is cancelled'
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle, status)
VALUES (5, 4, '[2026-01-01,2026-12-31)', 400000, 'quarterly', 'cancelled');
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle, status)
VALUES (5, 5, '[2026-02-01,2027-02-01)', 400000, 'quarterly', 'active');

\echo '--- T4 EXPECT ERROR: second active facility manager for Palm Court (partial unique index)'
INSERT INTO facility_assignment (building_id, user_id, role) VALUES (1, 1, 'facility_manager');

\echo '--- T5 EXPECT ERROR: duplicate email differing only by case'
INSERT INTO app_user (email, full_name) VALUES ('AMAKA.OBI@example.com', 'Duplicate Amaka');

\echo '--- T6 EXPECT ERROR: unit using a category from a different building (composite FK)'
INSERT INTO unit (building_id, category_id, label) VALUES (1, 3, 'X9');

\echo '--- T7 EXPECT ERROR: duplicate gateway reference (idempotency key)'
INSERT INTO payment (tenancy_id, payment_reference, amount, channel)
VALUES (2, 'PSK-0002-SAMPLE', 200000, 'card');

\echo '--- T8 EXPECT ERROR: succeeded payment without confirmation time'
UPDATE payment SET status = 'succeeded' WHERE payment_reference = 'PSK-0004-SAMPLE';

\echo '--- T9 EXPECT ERROR: allocating tenancy 2''s payment to tenancy 1''s rent cycle'
INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount) VALUES (2, 1, 2, 1000);
INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount) VALUES (2, 1, 1, 1000);

\echo '--- T10 EXPECT ERROR: overlapping rent cycle for the same tenancy'
INSERT INTO billing_cycle (tenancy_id, charge_type, period, amount_due, due_date)
VALUES (2, 'rent', '[2026-05-15,2026-06-15)', 200000, '2026-05-15');

\echo '--- T11 EXPECT ERROR: requisition charged to another building''s budget line'
INSERT INTO requisition (building_id, budget_line_id, raised_by, purpose)
VALUES (2, 1, 2, 'Wrong building');

\echo '--- T12 EXPECT ERROR: negative rent / invalid status / open-ended lease (CHECK constraints)'
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle)
VALUES (2, 4, '[2026-01-01,2027-01-01)', -5, 'annual');
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle, status)
VALUES (2, 4, '[2026-01-01,2027-01-01)', 500000, 'annual', 'squatting');
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle)
VALUES (2, 4, '[2026-01-01,)', 500000, 'annual');

\echo '--- T13 EXPECT ERROR: payout whose platform fee exceeds the amount'
INSERT INTO payout_request (building_id, requested_by, reference, amount, platform_fee)
VALUES (1, 2, 'PO-BAD-FEE', 1000, 5000);

\echo '--- T14 EXPECT ERROR: deleting a user who is still a tenant (FK, RESTRICT by default)'
DELETE FROM app_user WHERE user_id = 4;

\echo '--- Derived views (balances computed, never stored)'
SELECT cycle_id, tenancy_id, charge_type, period, amount_due, amount_paid, balance
FROM v_cycle_balance ORDER BY cycle_id;
SELECT * FROM v_building_wallet ORDER BY building_id;
SELECT * FROM v_budget_line_usage ORDER BY budget_line_id;
