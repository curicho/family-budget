"""CSV transaction importer.

Auto-detects common UK bank/statement export shapes:
  - single signed amount column  (HSBC, Monzo, Starling, Trading212 cash...)
  - separate money in / money out columns (Barclays, Lloyds, NatWest, HL...)
  - dd/mm/yyyy, yyyy-mm-dd and "12 Jan 2026" dates
Idempotent: every row gets a stable external_id (sha1 of date|amount|description|row-index-of-dupes),
and the file itself is deduped by sha256, so re-importing the same file is a no-op.
"""
import csv
import hashlib
import io
import re
from datetime import datetime

DATE_KEYS = ["date", "transaction date", "posting date", "booking date", "value date", "completed date"]
DESC_KEYS = ["description", "narrative", "details", "transaction description", "reference", "name", "merchant"]
AMOUNT_KEYS = ["amount", "value", "amount (gbp)", "transaction amount"]
MONEY_IN_KEYS = ["money in", "paid in", "credit", "credit amount", "in", "deposits"]
MONEY_OUT_KEYS = ["money out", "paid out", "debit", "debit amount", "out", "withdrawals"]

DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _find(header: list[str], keys: list[str]) -> int | None:
    normed = [_norm(h) for h in header]
    for k in keys:
        if k in normed:
            return normed.index(k)
    for i, h in enumerate(normed):          # fuzzy fallback
        if any(k in h for k in keys):
            return i
    return None


def _parse_date(s: str) -> str | None:
    s = (s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


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
    return -v if neg else v


def parse(content: bytes) -> dict:
    """Returns {mapping, rows:[{date, description, amount}], warnings}."""
    text = content.decode("utf-8-sig", errors="replace")
    # skip preamble lines some banks add before the real header
    lines = text.splitlines()
    reader = None
    header = None
    start = 0
    for i in range(min(10, len(lines))):
        candidate = list(csv.reader([lines[i]]))[0] if lines[i].strip() else []
        if len(candidate) >= 2 and _find(candidate, DATE_KEYS) is not None:
            header, start = candidate, i
            break
    if header is None:
        raise ValueError("could not find a header row containing a date column")

    reader = csv.reader(io.StringIO("\n".join(lines[start + 1:])))
    d_i = _find(header, DATE_KEYS)
    desc_i = _find(header, DESC_KEYS)
    amt_i = _find(header, AMOUNT_KEYS)
    in_i = _find(header, MONEY_IN_KEYS)
    out_i = _find(header, MONEY_OUT_KEYS)

    if amt_i is None and (in_i is None or out_i is None):
        raise ValueError(
            f"could not identify amount column(s) in header: {header}")

    rows, warnings, seen = [], [], {}
    for raw in reader:
        if not raw or all(not c.strip() for c in raw):
            continue
        date = _parse_date(raw[d_i]) if d_i < len(raw) else None
        if not date:
            warnings.append(f"skipped row (bad date): {raw[:3]}")
            continue
        desc = raw[desc_i].strip() if desc_i is not None and desc_i < len(raw) else "transaction"
        if amt_i is not None:
            amount = _parse_money(raw[amt_i] if amt_i < len(raw) else "")
        else:
            mi = _parse_money(raw[in_i] if in_i < len(raw) else "") or 0.0
            mo = _parse_money(raw[out_i] if out_i < len(raw) else "") or 0.0
            amount = mi - abs(mo)
        if amount is None:
            warnings.append(f"skipped row (bad amount): {raw[:3]}")
            continue
        key = f"{date}|{amount:.2f}|{desc}"
        seen[key] = seen.get(key, 0) + 1     # disambiguate identical rows
        ext = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
        rows.append({"date": date, "description": desc[:500],
                     "amount": round(amount, 2), "external_id": ext})

    return {
        "mapping": {
            "date": header[d_i],
            "description": header[desc_i] if desc_i is not None else None,
            "amount": header[amt_i] if amt_i is not None else None,
            "money_in": header[in_i] if in_i is not None else None,
            "money_out": header[out_i] if out_i is not None else None,
        },
        "rows": rows,
        "warnings": warnings[:20],
    }
