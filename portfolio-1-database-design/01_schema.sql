-- =====================================================================
-- MIT 8103 Portfolio 1: Database Design and Modelling
-- Case study: Lodgely (facility management + rent payments slice)
-- Target: PostgreSQL 16
--
-- A relational re-design of a document-oriented (MongoDB) system.
-- Run:  psql -d lodgely -f 01_schema.sql
-- =====================================================================

DROP SCHEMA IF EXISTS lodgely CASCADE;
CREATE SCHEMA lodgely;
SET search_path TO lodgely;

-- btree_gist lets an exclusion constraint mix "=" on a plain column
-- with "&&" (overlaps) on a date range.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------------
-- 1. Organisations and people
-- ---------------------------------------------------------------------

CREATE TABLE organisation (
    organisation_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             VARCHAR(150) NOT NULL,
    handle           VARCHAR(60)  UNIQUE,              -- optional, unique when present
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT chk_org_handle_format CHECK (handle ~ '^[a-z0-9-]{3,60}$')
);

CREATE TABLE app_user (
    user_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       VARCHAR(254) NOT NULL,
    full_name   VARCHAR(150) NOT NULL,
    phone       VARCHAR(20),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,                           -- soft delete
    CONSTRAINT chk_user_email_format CHECK (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$')
);
-- Case-insensitive uniqueness: 'Ada@x.com' and 'ada@x.com' are the same user.
CREATE UNIQUE INDEX uq_app_user_email_ci ON app_user (lower(email));

-- Replaces the embedded Organization.members[] array (M:N user <-> organisation).
CREATE TABLE organisation_member (
    organisation_id  BIGINT      NOT NULL REFERENCES organisation (organisation_id) ON DELETE CASCADE,
    user_id          BIGINT      NOT NULL REFERENCES app_user (user_id),
    org_role         VARCHAR(20) NOT NULL DEFAULT 'member',
    joined_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organisation_id, user_id),
    CONSTRAINT chk_member_role CHECK (org_role IN ('owner', 'admin', 'member'))
);

-- ---------------------------------------------------------------------
-- 2. Property structure
-- ---------------------------------------------------------------------

CREATE TABLE building (
    building_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id  BIGINT       NOT NULL REFERENCES organisation (organisation_id),
    name             VARCHAR(150) NOT NULL,
    address_line     VARCHAR(255) NOT NULL,
    city             VARCHAR(80)  NOT NULL,
    state            VARCHAR(80)  NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (organisation_id, name)
);

CREATE TABLE unit_category (
    category_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    building_id   BIGINT       NOT NULL REFERENCES building (building_id) ON DELETE CASCADE,
    name          VARCHAR(80)  NOT NULL,               -- e.g. '2-Bedroom Flat'
    bedrooms      SMALLINT     NOT NULL CHECK (bedrooms BETWEEN 0 AND 10),
    UNIQUE (building_id, name),
    UNIQUE (category_id, building_id)                  -- target for the composite FK below
);

-- Replaces the embedded Building.units[] array.
CREATE TABLE unit (
    unit_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    building_id   BIGINT      NOT NULL REFERENCES building (building_id) ON DELETE CASCADE,
    category_id   BIGINT      NOT NULL,
    label         VARCHAR(30) NOT NULL,                -- e.g. 'Block A, Flat 3'
    floor_no      SMALLINT,
    UNIQUE (building_id, label),
    -- Composite FK: a unit's category must belong to the SAME building.
    FOREIGN KEY (category_id, building_id)
        REFERENCES unit_category (category_id, building_id)
);

-- Staff roles per building (facility_manager / finance / viewer).
CREATE TABLE facility_assignment (
    assignment_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    building_id    BIGINT      NOT NULL REFERENCES building (building_id) ON DELETE CASCADE,
    user_id        BIGINT      NOT NULL REFERENCES app_user (user_id),
    role           VARCHAR(20) NOT NULL,
    status         VARCHAR(10) NOT NULL DEFAULT 'active',
    assigned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_fa_role   CHECK (role IN ('facility_manager', 'finance', 'viewer')),
    CONSTRAINT chk_fa_status CHECK (status IN ('active', 'revoked'))
);
-- At most ONE active facility manager per building (partial unique index).
CREATE UNIQUE INDEX uq_one_active_fm_per_building
    ON facility_assignment (building_id)
    WHERE role = 'facility_manager' AND status = 'active';
-- A user cannot hold the same active role twice on one building.
CREATE UNIQUE INDEX uq_active_role_per_user
    ON facility_assignment (building_id, user_id, role)
    WHERE status = 'active';

-- ---------------------------------------------------------------------
-- 3. Tenancies (leases)
-- ---------------------------------------------------------------------

CREATE TABLE tenancy (
    tenancy_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_id         BIGINT        NOT NULL REFERENCES unit (unit_id),
    tenant_user_id  BIGINT        NOT NULL REFERENCES app_user (user_id),
    lease_period    DATERANGE     NOT NULL,            -- [start, end)
    rent_amount     NUMERIC(14,2) NOT NULL,            -- per billing cycle, NGN
    rent_cycle      VARCHAR(10)   NOT NULL,
    service_charge  NUMERIC(14,2) NOT NULL DEFAULT 0,
    status          VARCHAR(12)   NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_rent_positive    CHECK (rent_amount > 0),
    CONSTRAINT chk_sc_nonnegative   CHECK (service_charge >= 0),
    CONSTRAINT chk_rent_cycle       CHECK (rent_cycle IN ('monthly', 'quarterly', 'annual')),
    CONSTRAINT chk_tenancy_status   CHECK (status IN ('pending', 'active', 'ended', 'cancelled')),
    CONSTRAINT chk_lease_bounded    CHECK (NOT lower_inf(lease_period) AND NOT upper_inf(lease_period)),
    -- No two live tenancies on the same unit may overlap in time.
    -- Stronger than the original system's partial unique index, which
    -- only allows one live tenancy per unit regardless of dates.
    CONSTRAINT ex_no_overlapping_tenancy
        EXCLUDE USING gist (unit_id WITH =, lease_period WITH &&)
        WHERE (status IN ('pending', 'active'))
);

-- ---------------------------------------------------------------------
-- 4. Billing and payments
-- ---------------------------------------------------------------------

-- One row per amount owed for one period. Replaces the stored running
-- balance in Ledger + ArchivedLedgerCycle: balances are DERIVED (see views).
CREATE TABLE billing_cycle (
    cycle_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenancy_id    BIGINT        NOT NULL REFERENCES tenancy (tenancy_id),
    charge_type   VARCHAR(15)   NOT NULL,
    period        DATERANGE     NOT NULL,
    amount_due    NUMERIC(14,2) NOT NULL,
    due_date      DATE          NOT NULL,
    CONSTRAINT chk_charge_type CHECK (charge_type IN ('rent', 'service_charge')),
    CONSTRAINT chk_amount_due  CHECK (amount_due > 0),
    UNIQUE (cycle_id, tenancy_id),                     -- target for composite FK
    -- A tenancy cannot be billed twice for overlapping periods of the same charge.
    CONSTRAINT ex_no_overlapping_cycle
        EXCLUDE USING gist (tenancy_id WITH =, charge_type WITH =, period WITH &&)
);

-- One row per payment-gateway charge attempt (e.g. Paystack).
CREATE TABLE payment (
    payment_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenancy_id         BIGINT        NOT NULL REFERENCES tenancy (tenancy_id),
    payment_reference  VARCHAR(64)   NOT NULL UNIQUE,  -- gateway reference = idempotency key
    amount             NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    channel            VARCHAR(15)   NOT NULL,
    status             VARCHAR(10)   NOT NULL DEFAULT 'pending',
    initiated_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    confirmed_at       TIMESTAMPTZ,
    CONSTRAINT chk_payment_channel CHECK (channel IN ('card', 'bank_transfer', 'ussd')),
    CONSTRAINT chk_payment_status  CHECK (status IN ('pending', 'succeeded', 'failed')),
    -- A succeeded payment must have a confirmation time; others must not.
    CONSTRAINT chk_confirmed_iff_succeeded
        CHECK ((status = 'succeeded') = (confirmed_at IS NOT NULL)),
    UNIQUE (payment_id, tenancy_id)                    -- target for composite FK
);

-- Resolves the M:N between payments and billing cycles: one payment can
-- clear several cycles (arrears first); one cycle can be paid in parts.
CREATE TABLE payment_allocation (
    payment_id  BIGINT        NOT NULL,
    cycle_id    BIGINT        NOT NULL,
    tenancy_id  BIGINT        NOT NULL,               -- carried for the composite FKs only
    amount      NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    PRIMARY KEY (payment_id, cycle_id),
    -- A payment can only be allocated to cycles of the SAME tenancy.
    FOREIGN KEY (payment_id, tenancy_id) REFERENCES payment (payment_id, tenancy_id),
    FOREIGN KEY (cycle_id, tenancy_id)   REFERENCES billing_cycle (cycle_id, tenancy_id)
);

-- A building manager withdrawing collected funds.
CREATE TABLE payout_request (
    payout_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    building_id   BIGINT        NOT NULL REFERENCES building (building_id),
    requested_by  BIGINT        NOT NULL REFERENCES app_user (user_id),
    reference     VARCHAR(64)   NOT NULL UNIQUE,
    amount        NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    platform_fee  NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (platform_fee >= 0),
    status        VARCHAR(10)   NOT NULL DEFAULT 'pending',
    requested_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_payout_status CHECK (status IN ('pending', 'paid', 'cancelled', 'failed')),
    CONSTRAINT chk_fee_le_amount CHECK (platform_fee <= amount)
);

-- ---------------------------------------------------------------------
-- 5. Budgets and maintenance requisitions
-- ---------------------------------------------------------------------

CREATE TABLE budget (
    budget_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    building_id  BIGINT      NOT NULL REFERENCES building (building_id) ON DELETE CASCADE,
    fiscal_year  SMALLINT    NOT NULL CHECK (fiscal_year BETWEEN 2000 AND 2100),
    UNIQUE (building_id, fiscal_year),
    UNIQUE (budget_id, building_id)                    -- target for composite FK
);

CREATE TABLE budget_line (
    budget_line_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    budget_id        BIGINT        NOT NULL,
    building_id      BIGINT        NOT NULL,           -- carried for the composite FKs only
    item_name        VARCHAR(100)  NOT NULL,           -- e.g. 'Generator maintenance'
    expected_amount  NUMERIC(14,2) NOT NULL CHECK (expected_amount > 0),
    UNIQUE (budget_id, item_name),
    UNIQUE (budget_line_id, building_id),
    FOREIGN KEY (budget_id, building_id) REFERENCES budget (budget_id, building_id) ON DELETE CASCADE
);

CREATE TABLE requisition (
    requisition_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    building_id     BIGINT       NOT NULL REFERENCES building (building_id),
    budget_line_id  BIGINT,                            -- optional: unbudgeted spend allowed
    raised_by       BIGINT       NOT NULL REFERENCES app_user (user_id),
    purpose         VARCHAR(255) NOT NULL,
    status          VARCHAR(10)  NOT NULL DEFAULT 'draft',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT chk_req_status CHECK (status IN ('draft', 'submitted', 'approved', 'rejected', 'paid')),
    -- The budget line charged must belong to the requisition's own building.
    FOREIGN KEY (budget_line_id, building_id) REFERENCES budget_line (budget_line_id, building_id)
);

CREATE TABLE requisition_item (
    requisition_id  BIGINT        NOT NULL REFERENCES requisition (requisition_id) ON DELETE CASCADE,
    line_no         SMALLINT      NOT NULL CHECK (line_no > 0),
    description     VARCHAR(150)  NOT NULL,
    quantity        NUMERIC(10,2) NOT NULL CHECK (quantity > 0),
    unit_price      NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (requisition_id, line_no)              -- weak entity: identified via its parent
);

-- ---------------------------------------------------------------------
-- 6. Derived data as views (instead of stored, drift-prone totals)
-- ---------------------------------------------------------------------

CREATE VIEW v_cycle_balance AS
SELECT bc.cycle_id,
       bc.tenancy_id,
       bc.charge_type,
       bc.period,
       bc.due_date,
       bc.amount_due,
       COALESCE(SUM(pa.amount), 0)                 AS amount_paid,
       bc.amount_due - COALESCE(SUM(pa.amount), 0) AS balance
FROM billing_cycle bc
LEFT JOIN payment_allocation pa ON pa.cycle_id = bc.cycle_id
GROUP BY bc.cycle_id;

CREATE VIEW v_budget_line_usage AS
SELECT bl.budget_line_id,
       bl.building_id,
       bl.item_name,
       bl.expected_amount,
       COALESCE(SUM(ri.quantity * ri.unit_price)
                FILTER (WHERE r.status IN ('approved', 'paid')), 0)::NUMERIC(14,2) AS committed_amount
FROM budget_line bl
LEFT JOIN requisition r       ON r.budget_line_id = bl.budget_line_id
LEFT JOIN requisition_item ri ON ri.requisition_id = r.requisition_id
GROUP BY bl.budget_line_id;

CREATE VIEW v_building_wallet AS
WITH income AS (
    SELECT u.building_id, SUM(p.amount) AS total_income
    FROM payment p
    JOIN tenancy t ON t.tenancy_id = p.tenancy_id
    JOIN unit u    ON u.unit_id    = t.unit_id
    WHERE p.status = 'succeeded'
    GROUP BY u.building_id
), outgoing AS (
    SELECT building_id,
           SUM(amount)       AS total_withdrawn,
           SUM(platform_fee) AS total_fees
    FROM payout_request
    WHERE status IN ('pending', 'paid')               -- pending payouts already reserve funds
    GROUP BY building_id
)
SELECT b.building_id,
       b.name,
       COALESCE(i.total_income, 0)    AS total_income,
       COALESCE(o.total_withdrawn, 0) AS total_withdrawn,
       COALESCE(o.total_fees, 0)      AS total_fees,
       COALESCE(i.total_income, 0) - COALESCE(o.total_withdrawn, 0) AS available_balance
FROM building b
LEFT JOIN income   i ON i.building_id = b.building_id
LEFT JOIN outgoing o ON o.building_id = b.building_id;

-- ---------------------------------------------------------------------
-- 7. Foreign-key indexes (Postgres does NOT index FK columns automatically)
-- ---------------------------------------------------------------------
CREATE INDEX ix_member_user        ON organisation_member (user_id);
CREATE INDEX ix_unit_category      ON unit (category_id);
CREATE INDEX ix_fa_user            ON facility_assignment (user_id);
CREATE INDEX ix_tenancy_tenant     ON tenancy (tenant_user_id);
CREATE INDEX ix_payment_tenancy    ON payment (tenancy_id);
CREATE INDEX ix_alloc_cycle        ON payment_allocation (cycle_id);
CREATE INDEX ix_payout_building    ON payout_request (building_id);
CREATE INDEX ix_req_building       ON requisition (building_id);
CREATE INDEX ix_req_budget_line    ON requisition (budget_line_id);
-- (unit_id on tenancy and tenancy_id on billing_cycle are already covered
--  by the GiST indexes behind the exclusion constraints.)
-- Query-specific indexes are deliberately left for Portfolio 2, so the
-- before/after effect of each can be measured.
