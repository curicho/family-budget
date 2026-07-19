# Payslip parser — detailed spec

## Pipeline

```
upload (PDF) ─► dedupe (sha256) ─► store in MinIO ─► parse job (queue)
   parse job:
     1. text extraction   pdfplumber; if <50 chars/page → rasterise + Tesseract OCR
     2. template match    employer templates (regex packs), ordered by confidence
     3. LLM fallback      Claude API, strict JSON schema, only if no template ≥0.9
     4. validation        arithmetic + sanity checks (below)
     5. persist           payslip row, status=pending_review
review UI ─► user confirms/corrects ─► posting engine writes ledger entries
```

## Target JSON schema (extraction output)

```json
{
  "employer": "Acme Ltd",
  "pay_date": "2026-06-30",
  "period_start": "2026-06-01",
  "period_end": "2026-06-30",
  "tax_code": "1257L",
  "gross_pay": 5000.00,
  "taxable_pay": 4750.00,
  "income_tax": 786.20,
  "employee_ni": 314.51,
  "employer_ni": 621.30,
  "pension_employee": 250.00,
  "pension_employer": 400.00,
  "pension_scheme_type": "salary_sacrifice",
  "student_loan": 180.00,
  "student_loan_plan": "plan_2",
  "other_deductions": [{"label": "Cycle to Work", "amount": 62.50}],
  "net_pay": 3406.79,
  "ytd": {"gross": 15000.00, "tax": 2358.60, "ni": 943.53, "pension": 750.00},
  "confidence": 0.97
}
```

## Validation rules (run regardless of parse method)

1. **Net pay arithmetic**: `gross − tax − NI − employee pension (if net-pay/sal-sac shown as deduction) − student loan − other = net`, tolerance ±£0.05. Failure → confidence capped at 0.5 and flagged in the review UI with the discrepancy shown.
2. **Salary sacrifice detection**: if `taxable_pay ≈ gross − pension_employee`, infer `salary_sacrifice`; if pension deducted after tax, infer `relief_at_source`. Show the inference in the UI — this affects the "pension is a transfer, not an expense" posting and effective-tax-rate insight.
3. **Plausibility bands**: tax ∈ [0, 47%·gross], NI ∈ [0, 15%·gross], net ∈ (0, gross]. Tax code matches `^[SC]?\d{1,4}[LMNTK]$|^(BR|D0|D1|NT|0T)$`.
4. **Duplicate guard**: `(member, pay_date, gross)` unique — re-uploading the same slip is a no-op with a friendly message.
5. **YTD continuity** (soft check): YTD gross ≥ previous slip's YTD gross within the same tax year (6 Apr boundary resets).

## Template matcher

A template is a YAML file per employer/format:

```yaml
name: acme-ltd-sdworx
detect:                       # ALL must match for the template to apply
  - contains: "Acme Ltd"
  - regex: "Tax Code[:\\s]+"
fields:
  gross_pay:   { regex: "Total Gross Pay\\s+£?([\\d,]+\\.\\d{2})" }
  income_tax:  { regex: "PAYE Tax\\s+£?([\\d,]+\\.\\d{2})" }
  employee_ni: { regex: "National Insurance\\s+£?([\\d,]+\\.\\d{2})" }
  # ...
```

Templates live in the DB (editable in UI) and are created semi-automatically: after the LLM successfully parses a new format twice with user confirmation, the app offers to "lock in" a template generated from the LLM's field positions. Result: LLM cost trends to zero for your regular monthly slip.

## LLM fallback call

Model: `claude-sonnet-4-6`. Input: extracted text (never the raw PDF unless text extraction failed and OCR is low-confidence — then send the page image). System prompt pins the JSON schema, UK payslip semantics, and "return null for absent fields; never guess amounts". Temperature 0. The response is schema-validated (pydantic/zod); any validation error → one retry with the error appended; second failure → status `pending_review` with empty fields for manual entry. Cost: ~£0.01/slip, and only for unrecognised formats.

Privacy note: payslip text contains name/NI number. Strip the NI number and address lines before the API call (regex `[A-Z]{2}\d{6}[A-D]` and the address block) — the parser doesn't need them.

## Posting engine (on confirmation)

| Payslip field | Ledger effect |
|---|---|
| net_pay | income txn (Salary) in the member's current account, auto-matched to the actual bank credit within ±3 days/±£1 (so it isn't double-counted with the Open Banking feed — the bank txn is linked, not duplicated) |
| income_tax, employee_ni | tax-kind txns (informational; excluded from "spending") |
| pension_employee + pension_employer | transfer txns into `pension_account_id`, and a `valuation` nudge if that account's last valuation is stale |
| student_loan | its own category → the insights engine can project payoff date |
| other_deductions | user maps each label to a category once; mapping remembered |

## Review UI requirements

Side-by-side: original PDF (rendered) left, parsed fields right, each field click-to-edit, discrepancies highlighted amber. One button: **Confirm & post**. Median time to confirm a clean slip should be under 5 seconds.
