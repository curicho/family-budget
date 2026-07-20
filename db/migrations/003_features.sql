-- 003: budgets, payslip templates, nutmeg provider, seed category rules & children

ALTER TYPE provider_kind ADD VALUE IF NOT EXISTS 'csv_nutmeg';

-- Monthly category budgets (per calendar month, GBP)
CREATE TABLE IF NOT EXISTS category_budget (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id  UUID NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    year_month   CHAR(7) NOT NULL,            -- 'YYYY-MM'
    amount_gbp   NUMERIC(14,2) NOT NULL CHECK (amount_gbp >= 0),
    UNIQUE (category_id, year_month)
);

-- Employer payslip templates (regex packs; editable)
CREATE TABLE IF NOT EXISTS payslip_template (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    detect      JSONB NOT NULL DEFAULT '[]',  -- [{contains|regex: "..."}]
    fields      JSONB NOT NULL DEFAULT '{}',  -- {field: {regex: "..."}}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO category (id, name, kind)
SELECT gen_random_uuid(), 'Student Loan', 'tax'
WHERE NOT EXISTS (SELECT 1 FROM category WHERE name = 'Student Loan' AND parent_id IS NULL);

-- Child categories under Housing / Insurance / Healthcare / Kids
INSERT INTO category (parent_id, name, kind)
SELECT p.id, c.name, c.kind
FROM category p
CROSS JOIN (VALUES
  ('Housing', 'Mortgage', 'expense'),
  ('Housing', 'Rent', 'expense'),
  ('Housing', 'Council Tax', 'expense'),
  ('Housing', 'Energy', 'expense'),
  ('Housing', 'Water', 'expense'),
  ('Housing', 'Broadband', 'expense'),
  ('Insurance', 'Life', 'expense'),
  ('Insurance', 'Home', 'expense'),
  ('Insurance', 'Car', 'expense'),
  ('Insurance', 'Health', 'expense'),
  ('Healthcare', 'GP/Private', 'expense'),
  ('Healthcare', 'Dental', 'expense'),
  ('Healthcare', 'Optical', 'expense'),
  ('Healthcare', 'Prescriptions', 'expense'),
  ('Healthcare', 'Therapy', 'expense'),
  ('Kids', 'Activities', 'expense'),
  ('Kids', 'Childcare', 'expense'),
  ('Kids', 'Clothing', 'expense'),
  ('Kids', 'School', 'expense'),
  ('Transport', 'Fuel', 'expense'),
  ('Transport', 'Public transport', 'expense'),
  ('Subscriptions', 'Streaming', 'expense'),
  ('Subscriptions', 'Software', 'expense')
) AS c(parent_name, name, kind)
WHERE p.name = c.parent_name AND p.parent_id IS NULL
ON CONFLICT (parent_id, name) DO NOTHING;

-- Seed auto-categorisation rules (common UK merchants)
INSERT INTO category_rule (priority, match_field, match_kind, match_value, category_id, activity_tag)
SELECT 50, 'description', 'contains', r.match_value, c.id, r.activity_tag
FROM (VALUES
  ('TESCO', 'Groceries', NULL),
  ('SAINSBURY', 'Groceries', NULL),
  ('ASDA', 'Groceries', NULL),
  ('WAITROSE', 'Groceries', NULL),
  ('ALDI', 'Groceries', NULL),
  ('LIDL', 'Groceries', NULL),
  ('OCADO', 'Groceries', NULL),
  ('NETFLIX', 'Streaming', NULL),
  ('SPOTIFY', 'Streaming', NULL),
  ('DISNEY+', 'Streaming', NULL),
  ('DISNEY PLUS', 'Streaming', NULL),
  ('AMAZON PRIME', 'Streaming', NULL),
  ('APPLE.COM/BILL', 'Software', NULL),
  ('GOOGLE *YOUTUBE', 'Streaming', NULL),
  ('TV LICENCE', 'Subscriptions', NULL),
  ('BRITISH GAS', 'Energy', NULL),
  ('OCTOPUS ENERGY', 'Energy', NULL),
  ('EON NEXT', 'Energy', NULL),
  ('EDF ENERGY', 'Energy', NULL),
  ('THAMES WATER', 'Water', NULL),
  ('SEVERN TRENT', 'Water', NULL),
  ('BT GROUP', 'Broadband', NULL),
  ('VIRGIN MEDIA', 'Broadband', NULL),
  ('SKY DIGITAL', 'Broadband', NULL),
  ('TALKTALK', 'Broadband', NULL),
  ('COUNCIL TAX', 'Council Tax', NULL),
  ('DVLA', 'Transport', NULL),
  ('TFL TRAVEL', 'Public transport', NULL),
  ('TRANSPORT FOR LONDON', 'Public transport', NULL),
  ('SHELL', 'Fuel', NULL),
  ('BP ', 'Fuel', NULL),
  ('TESCO PETROL', 'Fuel', NULL),
  ('UBER', 'Transport', NULL),
  ('BOOTS', 'Healthcare', NULL),
  ('SUPERDRUG', 'Healthcare', NULL),
  ('SWIMMING', 'Activities', 'swimming'),
  ('PIANO', 'Activities', 'piano'),
  ('FOOTBALL', 'Activities', 'football'),
  ('GYMNASTICS', 'Activities', 'gymnastics'),
  ('DANCE CLASS', 'Activities', 'dance'),
  ('NURSERY', 'Childcare', NULL),
  ('CHILDCARE', 'Childcare', NULL),
  ('OFSTED', 'Childcare', NULL)
) AS r(match_value, cat_name, activity_tag)
JOIN category c ON c.name = r.cat_name
WHERE NOT EXISTS (
  SELECT 1 FROM category_rule cr
  WHERE cr.match_value = r.match_value AND cr.category_id = c.id
);

-- Default stale intervals for asset-like accounts (applied when NULL)
UPDATE account SET valuation_stale_after = INTERVAL '35 days'
WHERE type IN ('sipp', 'workplace_pension', 'isa', 'jisa', 'gia')
  AND valuation_stale_after IS NULL;

UPDATE account SET valuation_stale_after = INTERVAL '90 days'
WHERE type IN ('property', 'vehicle', 'other_asset')
  AND valuation_stale_after IS NULL;
