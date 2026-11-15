-- =====================================================================
-- MIT 8103 Portfolio 2: large-volume FICTIONAL data generator
-- Requires the Portfolio 1 schema:  psql -d lodgely -f ../portfolio-1-database-design/01_schema.sql
-- Then:                             psql -d lodgely -f 01_seed_large_data.sql
--
-- Approximate volumes (takes a few minutes on a laptop):
--   100,000 users | 1,000 buildings | 10,000 units | 60,000 tenancies
--   710,000 billing cycles | ~700,000 payments | ~675,000 allocations
--   69,000 payouts | ~29,000 requisitions | ~87,000 requisition items
-- =====================================================================
SET search_path TO lodgely;
\timing on
SELECT setseed(0.42);   -- reproducible "random" data

BEGIN;

-- ---------- organisations and users ----------
INSERT INTO organisation (name, handle)
SELECT 'Estate Company ' || g, 'estate-co-' || g
FROM generate_series(1, 200) g;

INSERT INTO app_user (email, full_name, phone, created_at)
SELECT lower(f) || '.' || lower(l) || g || '@example.com',
       f || ' ' || l,
       '+23480' || lpad(g::text, 8, '0'),
       timestamptz '2020-01-01' + (random() * interval '2000 days')
FROM generate_series(1, 100000) g,
LATERAL (SELECT (ARRAY['Amaka','Tunde','Ngozi','Musa','Chioma','Femi','Aisha','Emeka',
                       'Halima','Bola','Ifeoma','Yusuf','Kemi','Obinna','Zainab','Segun'])
                       [1 + (g * 7) % 16] AS f,
                (ARRAY['Obi','Bello','Eze','Ibrahim','Okafor','Adeyemi','Musa','Nwosu',
                       'Abubakar','Ogunleye','Okonkwo','Danjuma','Balogun','Uche','Lawal','Ade'])
                       [1 + (g * 13) % 16] AS l) n;

-- ---------- buildings, categories, units ----------
INSERT INTO building (organisation_id, name, address_line, city, state)
SELECT 1 + (g - 1) / 5,
       'Building ' || g,
       g || ' Sample Road',
       (ARRAY['Abuja','Lagos','Port Harcourt','Kano','Ibadan'])[1 + g % 5],
       (ARRAY['FCT','Lagos','Rivers','Kano','Oyo'])[1 + g % 5]
FROM generate_series(1, 1000) g;

INSERT INTO unit_category (building_id, name, bedrooms)
SELECT b, c.name, c.bedrooms
FROM generate_series(1, 1000) b,
     (VALUES ('2-Bedroom Flat', 2::smallint), ('3-Bedroom Flat', 3::smallint)) c(name, bedrooms);

INSERT INTO unit (building_id, category_id, label, floor_no)
SELECT uc.building_id, uc.category_id, uc.name || ' #' || n, (n % 4)::smallint
FROM unit_category uc, generate_series(1, 5) n;

INSERT INTO facility_assignment (building_id, user_id, role)
SELECT b, 1 + (b * 37) % 100000, 'facility_manager' FROM generate_series(1, 1000) b;

-- ---------- tenancies: one per unit per year, 2021-2026 (monthly rent) ----------
INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle,
                     service_charge, status, created_at)
SELECT u.unit_id,
       1 + floor(random() * 100000)::int,
       daterange(make_date(y, 1, 1), make_date(y + 1, 1, 1)),
       CASE WHEN uc.bedrooms = 2 THEN 150000 ELSE 220000 END
         * (1 + (y - 2021) * 0.08),                         -- yearly rent increase
       'monthly', 0,
       CASE WHEN y < 2026 THEN 'ended' ELSE 'active' END,
       make_date(y - 1, 12, 1)
FROM unit u
JOIN unit_category uc ON uc.category_id = u.category_id
CROSS JOIN generate_series(2021, 2026) y;

-- ---------- billing cycles: monthly, up to November 2026 ----------
INSERT INTO billing_cycle (tenancy_id, charge_type, period, amount_due, due_date)
SELECT t.tenancy_id, 'rent',
       daterange(m::date, (m + interval '1 month')::date),
       round(t.rent_amount, 2),
       m::date
FROM tenancy t,
LATERAL generate_series(lower(t.lease_period)::timestamp,
                        LEAST(upper(t.lease_period), date '2026-12-01')::timestamp - interval '1 month',
                        interval '1 month') m;

-- ---------- successful payments: ~97% of old cycles, ~85% of 2026 cycles ----------
CREATE TEMP TABLE paid_cycle AS
SELECT cycle_id, tenancy_id, amount_due, due_date
FROM billing_cycle
WHERE random() < CASE WHEN due_date >= date '2026-01-01' THEN 0.85 ELSE 0.97 END;

INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, initiated_at, confirmed_at)
SELECT tenancy_id,
       'PSK-' || cycle_id,
       amount_due,
       (ARRAY['card','bank_transfer','ussd'])[1 + floor(random() * 3)::int],
       'succeeded',
       ts, ts + interval '2 minutes'
FROM (SELECT *, due_date + random() * interval '20 days' AS ts FROM paid_cycle) p;

INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount)
SELECT p.payment_id, pc.cycle_id, pc.tenancy_id, pc.amount_due
FROM paid_cycle pc
JOIN payment p ON p.payment_reference = 'PSK-' || pc.cycle_id;

-- ---------- failed and still-pending attempts ----------
INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, initiated_at)
SELECT tenancy_id, 'PSKF-' || cycle_id, amount_due, 'card', 'failed',
       due_date + random() * interval '10 days'
FROM billing_cycle WHERE random() < 0.03;

INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, initiated_at)
SELECT bc.tenancy_id, 'PSKP-' || bc.cycle_id, bc.amount_due, 'ussd', 'pending',
       timestamptz '2026-10-01' + random() * interval '38 days'
FROM billing_cycle bc
WHERE bc.due_date >= date '2026-10-01'
  AND NOT EXISTS (SELECT 1 FROM paid_cycle pc WHERE pc.cycle_id = bc.cycle_id)
  AND random() < 0.5;

-- ---------- payouts: one per building per month ----------
INSERT INTO payout_request (building_id, requested_by, reference, amount, platform_fee,
                            status, requested_at)
SELECT b, 1 + (b * 37) % 100000,
       'PO-' || b || '-' || to_char(m, 'YYYYMM'),
       amt, round(amt * 0.01, 2),
       CASE WHEN m < timestamp '2026-10-01' THEN 'paid' ELSE 'pending' END,
       m + interval '5 days'
FROM generate_series(1, 1000) b,
     generate_series(timestamp '2021-02-01', timestamp '2026-10-01', interval '1 month') m,
     LATERAL (SELECT round((800000 + random() * 400000)::numeric, 2) AS amt) a;

-- ---------- budgets, budget lines, requisitions ----------
INSERT INTO budget (building_id, fiscal_year)
SELECT b, y FROM generate_series(1, 1000) b, generate_series(2021, 2026) y;

INSERT INTO budget_line (budget_id, building_id, item_name, expected_amount)
SELECT bu.budget_id, bu.building_id, i.name, i.amount
FROM budget bu,
     (VALUES ('Generator maintenance', 600000), ('Plumbing repairs', 300000),
             ('Cleaning and waste', 240000), ('Security', 900000)) i(name, amount);

INSERT INTO requisition (building_id, budget_line_id, raised_by, purpose, status, created_at)
SELECT bl.building_id, bl.budget_line_id, 1 + (bl.building_id * 37) % 100000,
       'Routine spend #' || n,
       (ARRAY['submitted','approved','approved','paid','rejected'])[1 + floor(random() * 5)::int],
       make_timestamptz(bu.fiscal_year, 1 + floor(random() * 11)::int, 1, 9, 0, 0)
FROM budget_line bl
JOIN budget bu ON bu.budget_id = bl.budget_id,
     generate_series(1, 2) n
WHERE random() < 0.6;

INSERT INTO requisition_item (requisition_id, line_no, description, quantity, unit_price)
SELECT r.requisition_id, n, 'Item ' || n,
       1 + floor(random() * 10), round((5000 + random() * 20000)::numeric, 2)
FROM requisition r, generate_series(1, 3) n;

COMMIT;

-- Refresh planner statistics after the bulk load.
VACUUM ANALYZE;

-- Row counts for the evidence folder.
SELECT 'app_user' AS tbl, count(*) FROM app_user
UNION ALL SELECT 'building', count(*) FROM building
UNION ALL SELECT 'unit', count(*) FROM unit
UNION ALL SELECT 'tenancy', count(*) FROM tenancy
UNION ALL SELECT 'billing_cycle', count(*) FROM billing_cycle
UNION ALL SELECT 'payment', count(*) FROM payment
UNION ALL SELECT 'payment_allocation', count(*) FROM payment_allocation
UNION ALL SELECT 'payout_request', count(*) FROM payout_request
UNION ALL SELECT 'requisition', count(*) FROM requisition
UNION ALL SELECT 'requisition_item', count(*) FROM requisition_item;
