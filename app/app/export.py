"""Full CSV/JSON export for a UK tax year."""
import csv
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.taxyear import tax_year_bounds


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"not serializable: {type(obj)}")


def _rows(conn, sql: str, params=()) -> list[dict]:
    return conn.execute(sql, params).fetchall()


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for row in rows:
        w.writerow({k: _json_default(v) if isinstance(v, (Decimal, date, datetime, UUID)) else v
                    for k, v in row.items()})
    return buf.getvalue().encode("utf-8")


def _gather(conn, start: date, end: date) -> dict[str, list[dict]]:
    return {
        "transactions": _rows(
            conn,
            """SELECT t.posted_at, t.amount, t.currency, t.description, t.merchant,
                      t.activity_tag, t.source,
                      c.name AS category, a.name AS account, m.name AS member
               FROM transaction t
               JOIN account a ON a.id = t.account_id
               LEFT JOIN category c ON c.id = t.category_id
               LEFT JOIN member m ON m.id = t.member_id
               WHERE t.posted_at BETWEEN %s AND %s
               ORDER BY t.posted_at, t.description""",
            (start, end),
        ),
        "valuations": _rows(
            conn,
            """SELECT v.as_of, v.value_gbp, v.note, a.name AS account
               FROM valuation v
               JOIN account a ON a.id = v.account_id
               WHERE v.as_of BETWEEN %s AND %s
               ORDER BY v.as_of, a.name""",
            (start, end),
        ),
        "payslips": _rows(
            conn,
            """SELECT p.pay_date, p.period_start, p.period_end, m.name AS member,
                      p.employer, p.gross_pay, p.taxable_pay, p.income_tax,
                      p.employee_ni, p.pension_employee, p.pension_employer,
                      p.student_loan, p.net_pay, p.tax_code, p.status
               FROM payslip p
               JOIN member m ON m.id = p.member_id
               WHERE p.pay_date BETWEEN %s AND %s
               ORDER BY p.pay_date""",
            (start, end),
        ),
        "policies": _rows(
            conn,
            """SELECT p.kind, p.provider, m.name AS policyholder, p.cover_amount,
                      p.premium, p.premium_freq, p.start_date, p.renewal_date, p.active
               FROM policy p
               LEFT JOIN member m ON m.id = p.policyholder_member_id
               WHERE p.active
                  OR (p.start_date IS NOT NULL AND p.start_date <= %s)
                  OR (p.renewal_date IS NOT NULL AND p.renewal_date >= %s)
               ORDER BY p.provider""",
            (end, start),
        ),
        "holdings": _rows(
            conn,
            """SELECT a.name AS account, i.symbol, i.name AS instrument,
                      i.kind AS instrument_kind, h.quantity, h.avg_cost, h.as_of
               FROM holding h
               JOIN account a ON a.id = h.account_id
               JOIN instrument i ON i.id = h.instrument_id
               WHERE h.as_of::date BETWEEN %s AND %s
               ORDER BY a.name, i.symbol""",
            (start, end),
        ),
        "recurring_payments": _rows(
            conn,
            """SELECT r.merchant, a.name AS account, r.cadence_days, r.current_amount,
                      r.first_seen, r.last_seen, r.amount_history
               FROM recurring_payment r
               JOIN account a ON a.id = r.account_id
               WHERE r.first_seen <= %s AND r.last_seen >= %s
               ORDER BY r.merchant""",
            (end, start),
        ),
        "snapshots": _rows(
            conn,
            """SELECT s.snap_date, a.name AS account, s.value_gbp
               FROM snapshot s
               JOIN account a ON a.id = s.account_id
               WHERE s.snap_date BETWEEN %s AND %s
               ORDER BY s.snap_date, a.name""",
            (start, end),
        ),
    }


def export_tax_year(conn, tax_year: str, fmt: str) -> tuple[bytes, str, str]:
    start, end = tax_year_bounds(tax_year)
    data = _gather(conn, start, end)
    slug = tax_year.replace("/", "-")

    if fmt == "json":
        content = json.dumps(
            {"tax_year": tax_year, "start": start.isoformat(), "end": end.isoformat(), **data},
            default=_json_default,
            indent=2,
        ).encode("utf-8")
        return content, "application/json", f"family-budget-{slug}.json"

    if fmt != "csv":
        raise ValueError(f"unsupported format: {fmt}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in data.items():
            zf.writestr(f"{name}.csv", _csv_bytes(rows))
        manifest = _csv_bytes([{"file": f"{k}.csv", "rows": len(v)} for k, v in data.items()])
        zf.writestr("manifest.csv", manifest)

    return buf.getvalue(), "application/zip", f"family-budget-{slug}.zip"
