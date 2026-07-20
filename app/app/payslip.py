"""Payslip PDF parser and posting engine.

Pipeline: extract text → template match → LLM fallback → validate.
On confirmation, posts ledger entries and links to existing bank credits when possible.
"""
import io
import json
import os
import re
from datetime import datetime, timedelta

import httpx
import pdfplumber

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"]
NI_RE = re.compile(r"\b[A-Z]{2}\d{6}[A-D]\b")
POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
ADDRESS_LINE_RE = re.compile(
    r"^\s*\d+\s+\w+.*\b(Road|Street|Lane|Avenue|Close|Drive|Way|Place|Court|Gardens|Crescent)\b",
    re.I,
)
TAX_CODE_RE = re.compile(r"^(?:[SC]?\d{1,4}[LMNTK]|BR|D0|D1|NT|0T)$", re.I)

REQUIRED_FIELDS = ("gross_pay", "net_pay", "income_tax", "employee_ni", "pay_date")
MONEY_FIELDS = {
    "gross_pay", "taxable_pay", "income_tax", "employee_ni", "employer_ni",
    "pension_employee", "pension_employer", "student_loan", "net_pay",
}
DATE_FIELDS = {"pay_date", "period_start", "period_end"}

DEFAULT_TEMPLATES = [
    {
        "name": "sdworx-generic",
        "detect": [
            {"contains": "SD Worx"},
            {"regex": r"Tax Code"},
        ],
        "fields": {
            "employer": {"regex": r"Employer[:\s]+(.+?)(?:\n|$)"},
            "pay_date": {"regex": r"Pay Date[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})"},
            "period_start": {"regex": r"Period Start[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})"},
            "period_end": {"regex": r"Period End[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})"},
            "tax_code": {"regex": r"Tax Code[:\s]+([SC]?\d{1,4}[LMNTK]|BR|D0|D1|NT|0T)"},
            "gross_pay": {"regex": r"(?:Total )?Gross Pay\s+£?\s*([\d,]+\.\d{2})"},
            "taxable_pay": {"regex": r"Taxable Pay\s+£?\s*([\d,]+\.\d{2})"},
            "income_tax": {"regex": r"PAYE Tax\s+£?\s*([\d,]+\.\d{2})"},
            "employee_ni": {"regex": r"(?:Employee )?National Insurance\s+£?\s*([\d,]+\.\d{2})"},
            "employer_ni": {"regex": r"Employer(?:'s)? National Insurance\s+£?\s*([\d,]+\.\d{2})"},
            "pension_employee": {"regex": r"(?:Employee )?Pension\s+£?\s*([\d,]+\.\d{2})"},
            "pension_employer": {"regex": r"Employer(?:'s)? Pension\s+£?\s*([\d,]+\.\d{2})"},
            "student_loan": {"regex": r"Student Loan\s+£?\s*([\d,]+\.\d{2})"},
            "net_pay": {"regex": r"Net Pay\s+£?\s*([\d,]+\.\d{2})"},
        },
    },
    {
        "name": "uk-payroll-generic",
        "detect": [
            {"regex": r"Tax Code[:\s]+"},
            {"regex": r"Net Pay"},
            {"regex": r"Gross Pay|PAYE"},
        ],
        "fields": {
            "employer": {"regex": r"(?:Company|Employer)[:\s]+(.+?)(?:\n|$)"},
            "pay_date": {"regex": r"(?:Pay Date|Payment Date)[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})"},
            "tax_code": {"regex": r"Tax Code[:\s]+([SC]?\d{1,4}[LMNTK]|BR|D0|D1|NT|0T)"},
            "gross_pay": {"regex": r"(?:Total )?Gross(?: Pay)?\s+£?\s*([\d,]+\.\d{2})"},
            "income_tax": {"regex": r"(?:Income )?Tax(?:/PAYE)?\s+£?\s*([\d,]+\.\d{2})"},
            "employee_ni": {"regex": r"(?:Employee )?(?:NI|National Insurance)\s+£?\s*([\d,]+\.\d{2})"},
            "pension_employee": {"regex": r"(?:Employee )?Pension(?: Contribution)?\s+£?\s*([\d,]+\.\d{2})"},
            "net_pay": {"regex": r"Net Pay\s+£?\s*([\d,]+\.\d{2})"},
        },
    },
]

LLM_MODELS = ("claude-sonnet-4-6", "claude-sonnet-4-20250514")

LLM_SCHEMA = """{
  "employer": string|null,
  "pay_date": "YYYY-MM-DD"|null,
  "period_start": "YYYY-MM-DD"|null,
  "period_end": "YYYY-MM-DD"|null,
  "tax_code": string|null,
  "gross_pay": number|null,
  "taxable_pay": number|null,
  "income_tax": number|null,
  "employee_ni": number|null,
  "employer_ni": number|null,
  "pension_employee": number|null,
  "pension_employer": number|null,
  "pension_scheme_type": "salary_sacrifice"|"net_pay"|"relief_at_source"|null,
  "student_loan": number|null,
  "student_loan_plan": string|null,
  "other_deductions": [{"label": string, "amount": number}],
  "net_pay": number|null,
  "ytd": {"gross": number, "tax": number, "ni": number, "pension": number}|null,
  "confidence": number
}"""


def _parse_money(s: str) -> float | None:
    s = (s or "").strip().replace("£", "").replace(",", "").replace("GBP", "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return round(-v if neg else v, 2)


def _parse_date(s: str) -> str | None:
    s = (s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _other_total(other: list | None) -> float:
    if not other:
        return 0.0
    return round(sum(float(d.get("amount") or 0) for d in other), 2)


def extract_text(pdf_bytes: bytes) -> str:
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
    return "\n\n".join(parts)


def strip_pii(text: str) -> str:
    text = NI_RE.sub("[NI REDACTED]", text)
    lines = []
    for line in text.splitlines():
        if POSTCODE_RE.search(line) or ADDRESS_LINE_RE.match(line):
            lines.append("[ADDRESS REDACTED]")
        else:
            lines.append(line)
    return "\n".join(lines)


def _detect_match(text: str, rule: dict) -> bool:
    if "contains" in rule:
        return rule["contains"].lower() in text.lower()
    if "regex" in rule:
        return re.search(rule["regex"], text, re.I | re.M) is not None
    return False


def _field_value(field: str, raw: str):
    if field in DATE_FIELDS:
        return _parse_date(raw)
    if field in MONEY_FIELDS:
        return _parse_money(raw)
    return raw.strip()


def match_template(text: str, templates: list[dict]) -> tuple[dict | None, float]:
    best: dict | None = None
    best_conf = 0.0

    for tmpl in templates:
        detect = tmpl.get("detect") or []
        if detect and not all(_detect_match(text, r) for r in detect):
            continue

        parsed: dict = {}
        fields = tmpl.get("fields") or {}
        for fname, spec in fields.items():
            m = re.search(spec["regex"], text, re.I | re.M)
            if m and m.lastindex:
                val = _field_value(fname, m.group(1))
                if val is not None:
                    parsed[fname] = val

        found = sum(1 for f in REQUIRED_FIELDS if parsed.get(f) is not None)
        if found == len(REQUIRED_FIELDS):
            optional = [f for f in fields if f not in REQUIRED_FIELDS]
            bonus = sum(0.01 for f in optional if parsed.get(f) is not None)
            conf = min(0.99, 0.95 + bonus)
        else:
            conf = round((found / len(REQUIRED_FIELDS)) * 0.89, 2)

        if conf > best_conf:
            best, best_conf = parsed, conf

    return best, best_conf


def _llm_request(text: str, extra: str = "") -> dict | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None

    system = (
        "You extract structured data from UK payslip text. "
        "Return ONLY valid JSON matching this schema — no markdown, no commentary. "
        "Use null for absent fields; never guess amounts.\n\n"
        f"Schema:\n{LLM_SCHEMA}"
    )
    user = f"Payslip text:\n\n{strip_pii(text)}"
    if extra:
        user += f"\n\nPrevious validation error — fix and retry:\n{extra}"

    payload = {
        "model": LLM_MODELS[0],
        "max_tokens": 2048,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    with httpx.Client(timeout=60) as client:
        for model in LLM_MODELS:
            payload["model"] = model
            r = client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            blocks = r.json().get("content") or []
            raw = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
    return None


def _normalize_parsed(parsed: dict) -> dict:
    out = dict(parsed)
    for field in MONEY_FIELDS:
        v = out.get(field)
        if isinstance(v, (int, float)):
            out[field] = round(float(v), 2)
        elif isinstance(v, str):
            m = _parse_money(v)
            if m is not None:
                out[field] = m
    for field in DATE_FIELDS:
        v = out.get(field)
        if v:
            d = _parse_date(str(v))
            if d:
                out[field] = d
    other = out.get("other_deductions")
    if isinstance(other, list):
        out["other_deductions"] = [
            {"label": str(d.get("label") or ""), "amount": round(float(d.get("amount") or 0), 2)}
            for d in other if isinstance(d, dict)
        ]
    return out


def parse_with_llm(text: str) -> dict | None:
    parsed = _llm_request(text)
    if parsed is None:
        return None
    err = _schema_errors(parsed)
    if err:
        parsed = _llm_request(text, err)
    return _normalize_parsed(parsed) if parsed else None


def _schema_errors(parsed: dict) -> str | None:
    issues = []
    for field in MONEY_FIELDS:
        v = parsed.get(field)
        if v is not None and not isinstance(v, (int, float)):
            issues.append(f"{field} must be a number or null")
    for field in DATE_FIELDS:
        v = parsed.get(field)
        if v is not None and _parse_date(str(v)) is None:
            issues.append(f"{field} must be ISO date or null")
    other = parsed.get("other_deductions")
    if other is not None and not isinstance(other, list):
        issues.append("other_deductions must be a list")
    return "; ".join(issues) if issues else None


def _infer_pension_scheme(parsed: dict) -> str | None:
    existing = parsed.get("pension_scheme_type")
    if existing:
        return existing
    gross = parsed.get("gross_pay")
    taxable = parsed.get("taxable_pay")
    pen = parsed.get("pension_employee") or 0.0
    if gross is None or taxable is None:
        return None
    if abs(taxable - (gross - pen)) <= 0.05:
        return "salary_sacrifice"
    if abs(taxable - gross) <= 0.05 and pen > 0:
        return "relief_at_source"
    return None


def _expected_net(parsed: dict, scheme: str | None) -> float | None:
    gross = parsed.get("gross_pay")
    if gross is None:
        return None
    tax = parsed.get("income_tax") or 0.0
    ni = parsed.get("employee_ni") or 0.0
    pen = parsed.get("pension_employee") or 0.0
    loan = parsed.get("student_loan") or 0.0
    other = _other_total(parsed.get("other_deductions"))
    if scheme == "salary_sacrifice":
        return round(gross - tax - ni - loan - other, 2)
    return round(gross - tax - ni - pen - loan - other, 2)


def validate(parsed: dict) -> dict:
    out = dict(parsed)
    gross = out.get("gross_pay")
    net = out.get("net_pay")
    tax = out.get("income_tax") or 0.0
    ni = out.get("employee_ni") or 0.0
    scheme = _infer_pension_scheme(out)
    if scheme and not out.get("pension_scheme_type"):
        out["pension_scheme_type"] = scheme

    expected = _expected_net(out, scheme)
    delta = round(net - expected, 2) if net is not None and expected is not None else None
    arithmetic_ok = delta is not None and abs(delta) <= 0.05

    plausibility: dict = {}
    issues: list[str] = []
    if gross is not None and gross > 0:
        if tax < 0 or tax > gross * 0.47:
            issues.append("income_tax outside plausible band")
        plausibility["tax_ok"] = 0 <= tax <= gross * 0.47
        if ni < 0 or ni > gross * 0.15:
            issues.append("employee_ni outside plausible band")
        plausibility["ni_ok"] = 0 <= ni <= gross * 0.15
        if net is not None and not (0 < net <= gross):
            issues.append("net_pay outside plausible band")
        plausibility["net_ok"] = net is not None and 0 < net <= gross
    tc = out.get("tax_code")
    if tc is not None:
        plausibility["tax_code_ok"] = bool(TAX_CODE_RE.match(str(tc).strip()))
        if not plausibility["tax_code_ok"]:
            issues.append("tax_code format unexpected")

    conf = float(out.get("confidence") or out.get("parse_confidence") or 0.85)
    if not arithmetic_ok:
        conf = min(conf, 0.5)
        if delta is not None:
            issues.append(f"net pay arithmetic off by £{delta:.2f}")

    out["confidence"] = round(conf, 2)
    out["validation"] = {
        "arithmetic_ok": arithmetic_ok,
        "arithmetic_delta": delta,
        "expected_net": expected,
        "pension_scheme_inferred": scheme,
        "plausibility": plausibility,
        "issues": issues,
    }
    return out


def _empty_result() -> dict:
    return {
        "employer": None,
        "pay_date": None,
        "period_start": None,
        "period_end": None,
        "tax_code": None,
        "gross_pay": None,
        "taxable_pay": None,
        "income_tax": None,
        "employee_ni": None,
        "employer_ni": None,
        "pension_employee": None,
        "pension_employer": None,
        "pension_scheme_type": None,
        "student_loan": None,
        "student_loan_plan": None,
        "other_deductions": [],
        "net_pay": None,
        "ytd": {},
        "confidence": 0.0,
        "parse_method": "manual",
        "parse_confidence": 0.0,
        "validation": {"arithmetic_ok": False, "issues": ["nothing parsed"]},
    }


def parse_pdf(pdf_bytes: bytes, templates: list[dict] | None = None) -> dict:
    templates = templates if templates is not None else DEFAULT_TEMPLATES
    text = extract_text(pdf_bytes)
    if not text.strip():
        return _empty_result()

    parsed, conf = match_template(text, templates)
    parse_method = "template"

    if conf < 0.9:
        llm = parse_with_llm(text)
        if llm:
            parsed = llm
            conf = float(llm.get("confidence") or 0.85)
            parse_method = "llm"

    if not parsed:
        return _empty_result()

    parsed = _normalize_parsed(parsed)
    validated = validate(parsed)
    final_conf = float(validated.get("confidence") or conf)
    validated["parse_method"] = parse_method
    validated["parse_confidence"] = final_conf
    validated.setdefault("other_deductions", [])
    validated.setdefault("ytd", {})
    return validated


def _category_id(conn, name: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM category WHERE name = %s AND parent_id IS NULL LIMIT 1",
        (name,),
    ).fetchone()
    return str(row["id"]) if row else None


def _find_bank_credit(conn, account_id: str, pay_date: str, amount: float) -> dict | None:
    lo = (datetime.fromisoformat(pay_date).date() - timedelta(days=3)).isoformat()
    hi = (datetime.fromisoformat(pay_date).date() + timedelta(days=3)).isoformat()
    return conn.execute(
        """SELECT id, amount, posted_at, source, category_id
           FROM transaction
           WHERE account_id = %s
             AND posted_at BETWEEN %s AND %s
             AND amount > 0
             AND ABS(amount - %s) <= 1
             AND source IN ('open_banking', 'csv_import', 'api_sync')
           ORDER BY ABS(amount - %s), ABS(posted_at - %s::date)
           LIMIT 1""",
        (account_id, lo, hi, amount, amount, pay_date),
    ).fetchone()


def post_confirmed(conn, payslip_row: dict, salary_account_id: str) -> dict:
    """Post ledger entries for a confirmed payslip. Returns action summary."""
    payslip_id = payslip_row["id"]
    member_id = payslip_row["member_id"]
    pay_date = str(payslip_row["pay_date"])
    doc_id = payslip_row.get("document_id")
    currency_row = conn.execute(
        "SELECT currency FROM account WHERE id = %s", (salary_account_id,)
    ).fetchone()
    currency = currency_row["currency"] if currency_row else "GBP"

    salary_cat = _category_id(conn, "Salary")
    tax_cat = _category_id(conn, "Income Tax")
    ni_cat = _category_id(conn, "National Insurance")
    loan_cat = _category_id(conn, "Student Loan")

    summary: dict = {"linked": [], "created": [], "skipped": []}
    net = float(payslip_row["net_pay"])

    match = _find_bank_credit(conn, salary_account_id, pay_date, net)
    if match:
        conn.execute(
            """UPDATE transaction
               SET category_id = COALESCE(%s, category_id),
                   member_id = %s,
                   document_id = COALESCE(%s, document_id),
                   categorised_by = 'user'
               WHERE id = %s""",
            (salary_cat, member_id, doc_id, match["id"]),
        )
        summary["linked"].append({"kind": "net_pay", "transaction_id": str(match["id"])})
    else:
        ext = f"payslip:{payslip_id}:net"
        res = conn.execute(
            """INSERT INTO transaction
                 (account_id, posted_at, amount, currency, description,
                  category_id, member_id, source, external_id, document_id, categorised_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'payslip', %s, %s, 'user')
               ON CONFLICT (account_id, source, external_id) DO NOTHING
               RETURNING id""",
            (salary_account_id, pay_date, net, currency,
             f"Salary — {payslip_row.get('employer') or 'payslip'}",
             salary_cat, member_id, ext, doc_id),
        ).fetchone()
        if res:
            summary["created"].append({"kind": "net_pay", "transaction_id": str(res["id"])})

    def _post_tax(kind: str, amount: float, cat_id: str | None, label: str) -> None:
        if not amount or not cat_id:
            summary["skipped"].append(kind)
            return
        ext = f"payslip:{payslip_id}:{kind}"
        res = conn.execute(
            """INSERT INTO transaction
                 (account_id, posted_at, amount, currency, description,
                  category_id, member_id, source, external_id, document_id, categorised_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'payslip', %s, %s, 'user')
               ON CONFLICT (account_id, source, external_id) DO NOTHING
               RETURNING id""",
            (salary_account_id, pay_date, -abs(amount), currency, label,
             cat_id, member_id, ext, doc_id),
        ).fetchone()
        if res:
            summary["created"].append({"kind": kind, "transaction_id": str(res["id"])})

    _post_tax("income_tax", float(payslip_row.get("income_tax") or 0),
              tax_cat, "Income tax (payslip)")
    _post_tax("employee_ni", float(payslip_row.get("employee_ni") or 0),
              ni_cat, "National Insurance (payslip)")

    loan = float(payslip_row.get("student_loan") or 0)
    if loan and loan_cat:
        _post_tax("student_loan", loan, loan_cat, "Student loan (payslip)")
    elif loan:
        summary["skipped"].append("student_loan")

    pension_acct = payslip_row.get("pension_account_id")
    pen_emp = float(payslip_row.get("pension_employee") or 0)
    pen_er = float(payslip_row.get("pension_employer") or 0)
    pen_total = round(pen_emp + pen_er, 2)
    if pension_acct and pen_total > 0:
        pen_cat = _category_id(conn, "Pension Contributions")
        ext_out = f"payslip:{payslip_id}:pension_out"
        ext_in = f"payslip:{payslip_id}:pension_in"
        conn.execute(
            """INSERT INTO transaction
                 (account_id, posted_at, amount, currency, description,
                  category_id, member_id, source, external_id, document_id, categorised_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'payslip', %s, %s, 'user')
               ON CONFLICT (account_id, source, external_id) DO NOTHING""",
            (salary_account_id, pay_date, -pen_total, currency,
             "Pension contribution (payslip)", pen_cat, member_id, ext_out, doc_id),
        )
        conn.execute(
            """INSERT INTO transaction
                 (account_id, posted_at, amount, currency, description,
                  category_id, member_id, source, external_id, document_id, categorised_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'payslip', %s, %s, 'user')
               ON CONFLICT (account_id, source, external_id) DO NOTHING""",
            (pension_acct, pay_date, pen_total, currency,
             "Pension contribution (payslip)", pen_cat, member_id, ext_in, doc_id),
        )
        summary["created"].append({"kind": "pension_transfer", "amount": pen_total})

    conn.execute(
        "UPDATE payslip SET status = 'confirmed' WHERE id = %s", (payslip_id,)
    )
    return summary
