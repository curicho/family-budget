-- Family Budget — Postgres 16 schema (v1)
-- GBP-first, UK tax-year aware. All money stored as NUMERIC(14,2) in account currency,
-- with GBP-converted values materialised in snapshots for fast reporting.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- Household & members
-- ---------------------------------------------------------------------------
CREATE TABLE household (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    base_ccy    CHAR(3) NOT NULL DEFAULT 'GBP',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE member (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES household(id),
    name         TEXT NOT NULL,
    is_child     BOOLEAN NOT NULL DEFAULT FALSE,
    date_of_birth DATE,                       -- used for JISA→ISA rollover reminders at 18
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-login auth (decision: one user, MFA)
CREATE TABLE app_user (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,            -- argon2id
    totp_secret_enc BYTEA,                    -- encrypted with FIELD_ENCRYPTION_KEY
    webauthn_creds  JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Accounts, ownership, providers
-- ---------------------------------------------------------------------------
CREATE TYPE account_type AS ENUM (
    'current','savings','credit_card',
    'isa','jisa','gia','sipp','workplace_pension',
    'crypto','property','vehicle','other_asset','liability'
);

CREATE TYPE provider_kind AS ENUM (
    'manual','gocardless','trading212','coinbase','csv_hl','csv_jpm','csv_generic'
);

CREATE TABLE provider_connection (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         provider_kind NOT NULL,
    display_name TEXT NOT NULL,
    credentials_enc BYTEA,                    -- provider tokens, app-layer encrypted
    consent_expires_at TIMESTAMPTZ,           -- Open Banking consent: created + 90 days
    last_sync_at TIMESTAMPTZ,
    last_sync_status TEXT,                    -- 'ok' | error summary
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id  UUID NOT NULL REFERENCES household(id),
    name          TEXT NOT NULL,
    type          account_type NOT NULL,
    currency      CHAR(3) NOT NULL DEFAULT 'GBP',
    connection_id UUID REFERENCES provider_connection(id),
    external_ref  TEXT,                       -- provider-side account id
    is_liability  BOOLEAN NOT NULL DEFAULT FALSE,
    valuation_stale_after INTERVAL,           -- e.g. '35 days' for pensions/property
    archived      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ownership with splits (joint accounts: two rows summing to 100)
CREATE TABLE account_owner (
    account_id UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    member_id  UUID NOT NULL REFERENCES member(id),
    share_pct  NUMERIC(5,2) NOT NULL DEFAULT 100 CHECK (share_pct > 0 AND share_pct <= 100),
    PRIMARY KEY (account_id, member_id)
);

-- ---------------------------------------------------------------------------
-- Categories & ledger
-- ---------------------------------------------------------------------------
CREATE TABLE category (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id UUID REFERENCES category(id),
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('income','expense','transfer','tax','pension')),
    UNIQUE (parent_id, name)
);

CREATE TYPE txn_source AS ENUM (
    'open_banking','payslip','csv_import','manual','api_sync'
);

CREATE TABLE transaction (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   UUID NOT NULL REFERENCES account(id),
    posted_at    DATE NOT NULL,
    amount       NUMERIC(14,2) NOT NULL,      -- negative = outflow
    currency     CHAR(3) NOT NULL,
    description  TEXT NOT NULL,
    merchant     TEXT,
    category_id  UUID REFERENCES category(id),
    member_id    UUID REFERENCES member(id),  -- "whose spend is this" tag
    activity_tag TEXT,                        -- kids activities: 'swimming', 'piano'...
    source       txn_source NOT NULL,
    external_id  TEXT,                        -- provider txn id → idempotent syncs
    document_id  UUID,                        -- FK added after document table
    categorised_by TEXT CHECK (categorised_by IN ('rule','learned','llm','user')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, source, external_id)
);
CREATE INDEX ON transaction (posted_at);
CREATE INDEX ON transaction (category_id, posted_at);
CREATE INDEX ON transaction (activity_tag) WHERE activity_tag IS NOT NULL;

CREATE TABLE category_rule (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    priority    INT NOT NULL DEFAULT 100,
    match_field TEXT NOT NULL CHECK (match_field IN ('description','merchant')),
    match_kind  TEXT NOT NULL CHECK (match_kind IN ('contains','regex','equals')),
    match_value TEXT NOT NULL,
    category_id UUID NOT NULL REFERENCES category(id),
    member_id   UUID REFERENCES member(id),
    activity_tag TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Documents (payslips, statements, policy docs, CSVs)
-- ---------------------------------------------------------------------------
CREATE TABLE document (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         TEXT NOT NULL CHECK (kind IN ('payslip','statement','policy','csv','other')),
    object_key   TEXT NOT NULL,               -- MinIO key
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,        -- dedupe re-uploads
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE transaction
    ADD CONSTRAINT transaction_document_fk FOREIGN KEY (document_id) REFERENCES document(id);

-- ---------------------------------------------------------------------------
-- Payslips (parsed, confirmed → ledger postings)
-- ---------------------------------------------------------------------------
CREATE TABLE payslip (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES document(id) UNIQUE,
    member_id     UUID NOT NULL REFERENCES member(id),
    employer      TEXT,
    pay_date      DATE NOT NULL,
    period_start  DATE,
    period_end    DATE,
    tax_code      TEXT,
    gross_pay     NUMERIC(14,2) NOT NULL,
    taxable_pay   NUMERIC(14,2),
    income_tax    NUMERIC(14,2) NOT NULL DEFAULT 0,
    employee_ni   NUMERIC(14,2) NOT NULL DEFAULT 0,
    employer_ni   NUMERIC(14,2),
    pension_employee NUMERIC(14,2) NOT NULL DEFAULT 0,
    pension_employer NUMERIC(14,2) NOT NULL DEFAULT 0,
    pension_scheme_type TEXT CHECK (pension_scheme_type IN
        ('salary_sacrifice','net_pay','relief_at_source')),
    student_loan  NUMERIC(14,2) NOT NULL DEFAULT 0,
    student_loan_plan TEXT,
    other_deductions JSONB NOT NULL DEFAULT '[]',   -- [{label, amount}]
    net_pay       NUMERIC(14,2) NOT NULL,
    ytd           JSONB NOT NULL DEFAULT '{}',      -- {gross, tax, ni, pension...}
    parse_method  TEXT NOT NULL CHECK (parse_method IN ('template','llm','manual')),
    parse_confidence NUMERIC(3,2),
    status        TEXT NOT NULL DEFAULT 'pending_review'
                  CHECK (status IN ('pending_review','confirmed','rejected')),
    pension_account_id UUID REFERENCES account(id),  -- where contributions post
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    -- Re-upload of the same PDF is idempotent via document.sha256 +
    -- payslip.document_id UNIQUE (see migrations/004_payslip_upload_fallback.sql).
);

-- ---------------------------------------------------------------------------
-- Holdings & prices (investments, crypto)
-- ---------------------------------------------------------------------------
CREATE TABLE instrument (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol   TEXT NOT NULL,                   -- ticker / ISIN / coin code
    name     TEXT,
    kind     TEXT NOT NULL CHECK (kind IN ('equity','etf','fund','bond','crypto','cash')),
    currency CHAR(3) NOT NULL,
    UNIQUE (symbol, kind)
);

CREATE TABLE holding (
    account_id    UUID NOT NULL REFERENCES account(id),
    instrument_id UUID NOT NULL REFERENCES instrument(id),
    quantity      NUMERIC(24,10) NOT NULL,
    avg_cost      NUMERIC(14,4),              -- in instrument ccy
    as_of         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, instrument_id)
);

CREATE TABLE price (
    instrument_id UUID NOT NULL REFERENCES instrument(id),
    price_date    DATE NOT NULL,
    price         NUMERIC(18,6) NOT NULL,     -- instrument ccy
    PRIMARY KEY (instrument_id, price_date)
);

CREATE TABLE fx_rate (
    ccy       CHAR(3) NOT NULL,
    rate_date DATE NOT NULL,
    gbp_rate  NUMERIC(18,8) NOT NULL,         -- 1 unit ccy = X GBP
    PRIMARY KEY (ccy, rate_date)
);

-- Manual valuations (pensions, property, vehicles, JPM JISA)
CREATE TABLE valuation (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES account(id),
    as_of      DATE NOT NULL,
    value_gbp  NUMERIC(14,2) NOT NULL,
    note       TEXT,
    UNIQUE (account_id, as_of)
);

-- ---------------------------------------------------------------------------
-- Nightly snapshots → net worth history (requirements 7 & 10)
-- ---------------------------------------------------------------------------
CREATE TABLE snapshot (
    snap_date  DATE NOT NULL,
    account_id UUID NOT NULL REFERENCES account(id),
    value_gbp  NUMERIC(14,2) NOT NULL,        -- negative for liabilities
    PRIMARY KEY (snap_date, account_id)
);
-- Per-member net worth = SUM(snapshot.value_gbp * account_owner.share_pct/100)

-- ---------------------------------------------------------------------------
-- Protection registry (life / home / car / health / income protection)
-- ---------------------------------------------------------------------------
CREATE TABLE policy (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          TEXT NOT NULL CHECK (kind IN
        ('life','critical_illness','income_protection','home_buildings',
         'home_contents','car','travel','private_medical','health_cash_plan','other')),
    provider      TEXT NOT NULL,
    policyholder_member_id UUID REFERENCES member(id),
    cover_amount  NUMERIC(14,2),
    premium       NUMERIC(14,2) NOT NULL,
    premium_freq  TEXT NOT NULL DEFAULT 'monthly' CHECK (premium_freq IN ('monthly','annual')),
    start_date    DATE,
    renewal_date  DATE,                       -- feeds re-quote insights
    document_id   UUID REFERENCES document(id),
    match_pattern TEXT,                       -- auto-link premiums in bank feed
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Health cash plan claims tracking
CREATE TABLE health_claim (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id      UUID NOT NULL REFERENCES policy(id),
    transaction_id UUID REFERENCES transaction(id),
    claimed_at     DATE,
    amount_claimed NUMERIC(14,2),
    amount_paid    NUMERIC(14,2),
    status         TEXT NOT NULL DEFAULT 'unclaimed'
                   CHECK (status IN ('unclaimed','submitted','paid','declined'))
);

-- ---------------------------------------------------------------------------
-- Insights
-- ---------------------------------------------------------------------------
CREATE TABLE insight (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT NOT NULL,                -- 'subscription_creep','renewal_due',...
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    est_saving_gbp NUMERIC(14,2),
    severity    INT NOT NULL DEFAULT 3,       -- 1 high … 5 low
    source      TEXT NOT NULL CHECK (source IN ('rule','llm')),
    ref         JSONB NOT NULL DEFAULT '{}',  -- links to txns/policies/accounts
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','dismissed','done')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE recurring_payment (            -- detected subscriptions
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant     TEXT NOT NULL,
    account_id   UUID NOT NULL REFERENCES account(id),
    cadence_days INT NOT NULL,
    current_amount NUMERIC(14,2) NOT NULL,
    first_seen   DATE NOT NULL,
    last_seen    DATE NOT NULL,
    amount_history JSONB NOT NULL DEFAULT '[]',   -- [{date, amount}] → creep detection
    UNIQUE (merchant, account_id)
);

-- ---------------------------------------------------------------------------
-- Seed: UK category tree (abridged)
-- ---------------------------------------------------------------------------
INSERT INTO category (id, name, kind) VALUES
  (gen_random_uuid(),'Salary','income'),
  (gen_random_uuid(),'Income Tax','tax'),
  (gen_random_uuid(),'National Insurance','tax'),
  (gen_random_uuid(),'Pension Contributions','pension'),
  (gen_random_uuid(),'Groceries','expense'),
  (gen_random_uuid(),'Housing','expense'),        -- children: Mortgage, Rent, Council Tax, Energy, Water, Broadband
  (gen_random_uuid(),'Insurance','expense'),      -- children per policy kind
  (gen_random_uuid(),'Healthcare','expense'),     -- children: GP/Private, Dental, Optical, Prescriptions, Therapy
  (gen_random_uuid(),'Kids','expense'),           -- children: Activities, Childcare, Clothing, School
  (gen_random_uuid(),'Transport','expense'),
  (gen_random_uuid(),'Subscriptions','expense'),
  (gen_random_uuid(),'Holidays','expense'),
  (gen_random_uuid(),'Transfers','transfer');
