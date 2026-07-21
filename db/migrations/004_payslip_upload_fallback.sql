-- Allow multiple pending/manual payslips for the same member/day when parse
-- fails (placeholders were pay_date=today, gross=0 and collided). Idempotent
-- re-upload is still guarded by document.sha256 + payslip.document_id UNIQUE.
ALTER TABLE payslip DROP CONSTRAINT IF EXISTS payslip_member_id_pay_date_gross_pay_key;
