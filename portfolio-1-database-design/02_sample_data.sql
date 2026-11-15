-- =====================================================================
-- Portfolio 1: small, entirely FICTIONAL sample dataset.
-- (Large-volume data for performance testing is generated in Portfolio 2.)
-- Run after 01_schema.sql:  psql -d lodgely -f 02_sample_data.sql
-- =====================================================================
SET search_path TO lodgely;

BEGIN;

INSERT INTO organisation (name, handle) VALUES
  ('Greenfield Estates Ltd', 'greenfield-estates'),
  ('Harbour Point Properties', 'harbour-point');

INSERT INTO app_user (email, full_name, phone) VALUES
  ('amaka.obi@example.com',      'Amaka Obi',      '+2348010000001'),  -- 1 org owner
  ('tunde.bello@example.com',    'Tunde Bello',    '+2348010000002'),  -- 2 facility manager
  ('ngozi.eze@example.com',      'Ngozi Eze',      '+2348010000003'),  -- 3 finance
  ('musa.ibrahim@example.com',   'Musa Ibrahim',   '+2348010000004'),  -- 4 tenant
  ('chioma.okafor@example.com',  'Chioma Okafor',  '+2348010000005'),  -- 5 tenant
  ('femi.adeyemi@example.com',   'Femi Adeyemi',   '+2348010000006');  -- 6 tenant

INSERT INTO organisation_member (organisation_id, user_id, org_role) VALUES
  (1, 1, 'owner'), (1, 2, 'member'), (1, 3, 'member');

INSERT INTO building (organisation_id, name, address_line, city, state) VALUES
  (1, 'Palm Court', '12 Example Crescent', 'Abuja', 'FCT'),
  (1, 'Cedar Heights', '4 Sample Close', 'Abuja', 'FCT');

INSERT INTO unit_category (building_id, name, bedrooms) VALUES
  (1, '2-Bedroom Flat', 2),   -- 1
  (1, '3-Bedroom Flat', 3),   -- 2
  (2, 'Studio', 0);           -- 3

INSERT INTO unit (building_id, category_id, label, floor_no) VALUES
  (1, 1, 'A1', 1), (1, 1, 'A2', 1), (1, 2, 'B1', 2),
  (2, 3, 'S1', 0), (2, 3, 'S2', 0);

INSERT INTO facility_assignment (building_id, user_id, role) VALUES
  (1, 2, 'facility_manager'),
  (1, 3, 'finance'),
  (2, 2, 'facility_manager');

INSERT INTO tenancy (unit_id, tenant_user_id, lease_period, rent_amount, rent_cycle, service_charge, status) VALUES
  (1, 4, '[2026-01-01,2027-01-01)', 1800000.00, 'annual',   250000.00, 'active'),   -- 1
  (3, 5, '[2026-03-01,2027-03-01)',  200000.00, 'monthly',   20000.00, 'active'),   -- 2
  (4, 6, '[2025-06-01,2026-06-01)',  450000.00, 'quarterly',      0.00, 'ended');   -- 3

INSERT INTO billing_cycle (tenancy_id, charge_type, period, amount_due, due_date) VALUES
  (1, 'rent',           '[2026-01-01,2027-01-01)', 1800000.00, '2026-01-01'),  -- 1
  (1, 'service_charge', '[2026-01-01,2027-01-01)',  250000.00, '2026-01-01'),  -- 2
  (2, 'rent',           '[2026-03-01,2026-04-01)',  200000.00, '2026-03-01'),  -- 3
  (2, 'rent',           '[2026-04-01,2026-05-01)',  200000.00, '2026-04-01'),  -- 4
  (2, 'rent',           '[2026-05-01,2026-06-01)',  200000.00, '2026-05-01');  -- 5

INSERT INTO payment (tenancy_id, payment_reference, amount, channel, status, confirmed_at) VALUES
  (1, 'PSK-0001-SAMPLE', 2050000.00, 'bank_transfer', 'succeeded', '2026-01-02 10:15+01'),  -- 1
  (2, 'PSK-0002-SAMPLE',  200000.00, 'card',          'succeeded', '2026-03-01 09:00+01'),  -- 2
  (2, 'PSK-0003-SAMPLE',  300000.00, 'card',          'succeeded', '2026-05-03 18:40+01'),  -- 3
  (2, 'PSK-0004-SAMPLE',  200000.00, 'ussd',          'pending',   NULL);                   -- 4

-- Payment 3 clears April's arrears first, then part of May (arrears-first rule).
INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount) VALUES
  (1, 1, 1, 1800000.00),
  (1, 2, 1,  250000.00),
  (2, 3, 2,  200000.00),
  (3, 4, 2,  200000.00),
  (3, 5, 2,  100000.00);

INSERT INTO payout_request (building_id, requested_by, reference, amount, platform_fee, status) VALUES
  (1, 2, 'PO-0001-SAMPLE', 1500000.00, 15000.00, 'paid');

INSERT INTO budget (building_id, fiscal_year) VALUES (1, 2026);

INSERT INTO budget_line (budget_id, building_id, item_name, expected_amount) VALUES
  (1, 1, 'Generator maintenance', 600000.00),
  (1, 1, 'Plumbing repairs',      300000.00);

INSERT INTO requisition (building_id, budget_line_id, raised_by, purpose, status) VALUES
  (1, 1, 2, 'Quarterly generator servicing', 'approved'),
  (1, 2, 2, 'Fix leaking riser in Block B',  'submitted');

INSERT INTO requisition_item (requisition_id, line_no, description, quantity, unit_price) VALUES
  (1, 1, 'Engine oil (litres)',  20,  6500.00),
  (1, 2, 'Technician labour',     1, 85000.00),
  (2, 1, 'PVC pipe 2-inch',       6, 12000.00);

COMMIT;
