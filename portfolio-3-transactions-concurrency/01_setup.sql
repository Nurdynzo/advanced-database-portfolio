-- =====================================================================
-- MIT 8103 Portfolio 3: setup for the transaction and concurrency demos.
-- Runs on top of the SMALL Portfolio 1 dataset (not the large P2 data), so
-- every number in the demos is easy to check by hand:
--   psql -d lodgely -f ../portfolio-1-database-design/01_schema.sql
--   psql -d lodgely -f ../portfolio-1-database-design/02_sample_data.sql
--   psql -d lodgely -f 01_setup.sql
-- =====================================================================
SET search_path TO lodgely;

-- ---------------------------------------------------------------------
-- Extra demo data
-- ---------------------------------------------------------------------

-- More monthly rent cycles for tenancy 2 (cycles 6-9: June-September 2026).
INSERT INTO billing_cycle (tenancy_id, charge_type, period, amount_due, due_date) VALUES
  (2, 'rent', '[2026-06-01,2026-07-01)', 200000.00, '2026-06-01'),
  (2, 'rent', '[2026-07-01,2026-08-01)', 200000.00, '2026-07-01'),
  (2, 'rent', '[2026-08-01,2026-09-01)', 200000.00, '2026-08-01'),
  (2, 'rent', '[2026-09-01,2026-10-01)', 200000.00, '2026-09-01');

-- Settle May's remaining N100,000 so tenancy 2's arrears start cleanly at
-- June: payment 3 (now N400,000) covers April + May in full.
UPDATE payment SET amount = 400000.00 WHERE payment_id = 3;
UPDATE payment_allocation SET amount = 200000.00 WHERE payment_id = 3 AND cycle_id = 5;

-- Two requisitions of N180,000 each against 'Plumbing repairs' (budget N300,000).
-- Either fits alone; both together overspend.
INSERT INTO requisition (building_id, budget_line_id, raised_by, purpose, status) VALUES
  (1, 2, 2, 'Replace water pump',       'submitted'),   -- requisition 3
  (1, 2, 2, 'Re-pipe Block A bathrooms','submitted');   -- requisition 4
INSERT INTO requisition_item (requisition_id, line_no, description, quantity, unit_price) VALUES
  (3, 1, 'Submersible pump, installed', 1, 180000.00),
  (4, 1, 'Bathroom re-piping (per flat)', 3, 60000.00);
-- Requisition 2 (N72,000, also on this line) is withdrawn so the budget line starts empty.
UPDATE requisition SET status = 'rejected' WHERE requisition_id = 2;

-- A second payout so two payouts can be locked in opposite orders (deadlock demo).
INSERT INTO payout_request (building_id, requested_by, reference, amount, platform_fee, status)
VALUES (1, 2, 'PO-0002-SAMPLE', 200000.00, 2000.00, 'pending');
UPDATE payout_request SET status = 'pending' WHERE reference = 'PO-0001-SAMPLE';

-- Optimistic concurrency control: a version number bumped on every update.
ALTER TABLE requisition_item ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------
-- Shared helper: allocate a payment to a tenancy's oldest unpaid cycles
-- first (arrears-first rule). Must run inside the caller's transaction.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION allocate_payment(p_payment_id BIGINT)
RETURNS NUMERIC LANGUAGE plpgsql AS $$
DECLARE
    v_tenancy   BIGINT;
    v_remaining NUMERIC;
    v_cycle     RECORD;
    v_part      NUMERIC;
BEGIN
    SELECT tenancy_id, amount INTO v_tenancy, v_remaining
    FROM payment WHERE payment_id = p_payment_id;

    FOR v_cycle IN
        SELECT cycle_id, balance FROM v_cycle_balance
        WHERE tenancy_id = v_tenancy AND balance > 0
        ORDER BY due_date
    LOOP
        EXIT WHEN v_remaining <= 0;
        v_part := LEAST(v_remaining, v_cycle.balance);
        INSERT INTO payment_allocation (payment_id, cycle_id, tenancy_id, amount)
        VALUES (p_payment_id, v_cycle.cycle_id, v_tenancy, v_part);
        v_remaining := v_remaining - v_part;
    END LOOP;
    RETURN v_remaining;   -- unallocated credit, if any
END $$;

-- ---------------------------------------------------------------------
-- Scenario 2: payment confirmation (gateway webhook and manual "verify"
-- can arrive at the same time for the same payment).
-- p_delay simulates the call to the gateway's verification API that sits
-- between the check and the write.
-- ---------------------------------------------------------------------

-- UNSAFE: read, check in code, then write (check-then-act).
CREATE OR REPLACE FUNCTION confirm_payment_naive(p_ref TEXT, p_delay NUMERIC DEFAULT 2)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_id     BIGINT;
    v_status TEXT;
BEGIN
    SELECT payment_id, status INTO v_id, v_status
    FROM payment WHERE payment_reference = p_ref;

    IF v_status = 'succeeded' THEN
        RETURN 'skipped: already succeeded';
    END IF;

    PERFORM pg_sleep(p_delay);                 -- verify with gateway

    UPDATE payment SET status = 'succeeded', confirmed_at = now()
    WHERE payment_id = v_id;
    PERFORM allocate_payment(v_id);
    RETURN 'credited';
END $$;

-- FIX A: the check and the write are ONE atomic statement. A concurrent
-- caller blocks on the row lock, then re-evaluates `status = 'pending'`
-- against the committed row, matches nothing, and stops.
CREATE OR REPLACE FUNCTION confirm_payment_atomic(p_ref TEXT, p_delay NUMERIC DEFAULT 2)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_id BIGINT;
BEGIN
    PERFORM pg_sleep(p_delay);                 -- verify with gateway (safe to repeat)

    UPDATE payment SET status = 'succeeded', confirmed_at = now()
    WHERE payment_reference = p_ref AND status = 'pending'
    RETURNING payment_id INTO v_id;

    IF NOT FOUND THEN
        RETURN 'skipped: already processed by another request';
    END IF;
    PERFORM allocate_payment(v_id);
    RETURN 'credited';
END $$;

-- FIX B: pessimistic lock. SELECT ... FOR UPDATE makes the second caller
-- wait BEFORE its check, so it sees the first caller's committed result.
CREATE OR REPLACE FUNCTION confirm_payment_locked(p_ref TEXT, p_delay NUMERIC DEFAULT 2)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_id     BIGINT;
    v_status TEXT;
BEGIN
    SELECT payment_id, status INTO v_id, v_status
    FROM payment WHERE payment_reference = p_ref
    FOR UPDATE;                                -- row lock held until COMMIT/ROLLBACK

    IF v_status = 'succeeded' THEN
        RETURN 'skipped: already succeeded';
    END IF;

    PERFORM pg_sleep(p_delay);

    UPDATE payment SET status = 'succeeded', confirmed_at = now()
    WHERE payment_id = v_id;
    PERFORM allocate_payment(v_id);
    RETURN 'credited';
END $$;

-- ---------------------------------------------------------------------
-- Scenario 3: approve a requisition only if the budget line has room.
-- ---------------------------------------------------------------------

-- Check-then-act. Correct under SERIALIZABLE, wrong under weaker levels.
CREATE OR REPLACE FUNCTION approve_requisition(p_req_id BIGINT, p_delay NUMERIC DEFAULT 2)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_line      BIGINT;
    v_total     NUMERIC;
    v_expected  NUMERIC;
    v_committed NUMERIC;
BEGIN
    SELECT r.budget_line_id, sum(ri.quantity * ri.unit_price)::NUMERIC(14,2)
    INTO v_line, v_total
    FROM requisition r JOIN requisition_item ri USING (requisition_id)
    WHERE r.requisition_id = p_req_id
    GROUP BY r.budget_line_id;

    SELECT expected_amount, committed_amount INTO v_expected, v_committed
    FROM v_budget_line_usage WHERE budget_line_id = v_line;

    IF v_committed + v_total > v_expected THEN
        UPDATE requisition SET status = 'rejected' WHERE requisition_id = p_req_id;
        RETURN format('rejected: %s committed + %s requested > %s budget',
                      v_committed, v_total, v_expected);
    END IF;

    PERFORM pg_sleep(p_delay);                 -- e.g. notify approver, audit log

    UPDATE requisition SET status = 'approved' WHERE requisition_id = p_req_id;
    RETURN format('approved: %s committed + %s requested <= %s budget',
                  v_committed, v_total, v_expected);
END $$;

-- Lock the budget line first ("materialising the conflict"): approvals on
-- the same line now queue up, even under READ COMMITTED.
CREATE OR REPLACE FUNCTION approve_requisition_locked(p_req_id BIGINT, p_delay NUMERIC DEFAULT 2)
RETURNS TEXT LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM budget_line bl
    JOIN requisition r ON r.budget_line_id = bl.budget_line_id
    WHERE r.requisition_id = p_req_id
    FOR UPDATE OF bl;
    RETURN approve_requisition(p_req_id, p_delay);
END $$;
