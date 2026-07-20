"""HL (Hargreaves Lansdown) and Nutmeg CSV/PDF importers for holdings and cash txns."""
import csv
import hashlib
import io
import re

from app.csvimport import DATE_KEYS, DESC_KEYS, _find, _norm, _parse_date, _parse_money

# --- format detection -------------------------------------------------------

HL_HOLDINGS_KEYS = ["stock", "investment", "fund name", "unit trust"]
HL_UNITS_KEYS = ["units", "quantity", "holding"]
HL_VALUE_KEYS = ["value (£)", "value", "market value", "value gbp"]
HL_COST_KEYS = ["cost (£)", "cost", "book cost"]
HL_PRICE_KEYS = ["price (p)", "price", "unit price"]

NUTMEG_PORTFOLIO_KEYS = ["portfolio", "portfolio name", "account name"]
NUTMEG_VALUE_KEYS = ["holdings value", "total value", "portfolio value", "value"]
NUTMEG_SYMBOL_KEYS = ["asset", "investment", "fund", "holding name"]

HL_TXN_DATE = DATE_KEYS
HL_TXN_DESC = DESC_KEYS + ["transaction", "details", "type"]
HL_TXN_AMOUNT = ["amount", "value", "amount (£)", "transaction amount"]
HL_TXN_IN = ["money in", "credit", "paid in"]
HL_TXN_OUT = ["money out", "debit", "paid out"]

NUTMEG_TXN_DATE = DATE_KEYS
NUTMEG_TXN_DESC = ["description", "transaction", "details", "narrative"]
NUTMEG_TXN_AMOUNT = ["amount", "value", "amount (£)"]


def detect_format(content: bytes, filename: str | None = None) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".pdf"):
        text = _extract_text(content).lower()
        if "nutmeg" in text or "nutmeg.com" in text:
            return "nutmeg_holdings" if "holdings" in text or "portfolio" in text else "nutmeg_transactions"
        if "hargreaves" in text or "hl.co.uk" in text or "hl client" in text:
            return "hl_holdings" if re.search(r"\bunits\b|\bstock\b", text) else "hl_transactions"
        return "generic"

    text = content.decode("utf-8-sig", errors="replace")
    header = _first_header(text)
    if not header:
        return "generic"
    normed = [_norm(h) for h in header]
    joined = " ".join(normed)

    if "nutmeg" in fn or any(k in joined for k in NUTMEG_PORTFOLIO_KEYS):
        if _find(header, NUTMEG_TXN_DATE) is not None and _find(header, NUTMEG_TXN_AMOUNT) is not None:
            return "nutmeg_transactions"
        return "nutmeg_holdings"
    if "hl" in fn or "hargreaves" in fn or _find(header, HL_HOLDINGS_KEYS) is not None:
        if _find(header, HL_TXN_DATE) is not None and (
                _find(header, HL_TXN_AMOUNT) is not None or _find(header, HL_TXN_IN) is not None):
            return "hl_transactions"
        return "hl_holdings"
    if _find(header, HL_UNITS_KEYS) is not None and _find(header, HL_VALUE_KEYS) is not None:
        return "hl_holdings"
    if _find(header, HL_TXN_DATE) is not None and _find(header, HL_TXN_AMOUNT) is not None:
        return "hl_transactions"
    return "generic"


def _extract_text(content: bytes) -> str:
    if content[:5] != b"%PDF-":
        return content.decode("utf-8-sig", errors="replace")
    try:
        import pdfplumber
        import io as _io
        parts = []
        with pdfplumber.open(_io.BytesIO(content)) as pdf:
            for page in pdf.pages[:5]:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return ""


def _first_header(text: str) -> list[str] | None:
    lines = text.splitlines()
    for i in range(min(15, len(lines))):
        row = list(csv.reader([lines[i]]))[0] if lines[i].strip() else []
        if len(row) >= 2:
            return row
    return None


def _read_csv_rows(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header, start = None, 0
    for i in range(min(15, len(lines))):
        candidate = list(csv.reader([lines[i]]))[0] if lines[i].strip() else []
        if len(candidate) >= 2:
            header, start = candidate, i
            break
    if header is None:
        raise ValueError("could not find CSV header row")
    rows = list(csv.reader(io.StringIO("\n".join(lines[start + 1:]))))
    return header, rows


def _parse_price_pence(raw: str) -> float | None:
    v = _parse_money(raw)
    if v is None:
        return None
    # HL "Price (p)" is in pence
    return round(v / 100, 4)


# --- HL parsers -------------------------------------------------------------

def parse_hl_holdings(content: bytes) -> list[dict]:
    header, rows = _read_csv_rows(content)
    stock_i = _find(header, HL_HOLDINGS_KEYS)
    units_i = _find(header, HL_UNITS_KEYS)
    value_i = _find(header, HL_VALUE_KEYS)
    cost_i = _find(header, HL_COST_KEYS)
    price_i = _find(header, HL_PRICE_KEYS)

    if stock_i is None or units_i is None:
        raise ValueError(f"HL holdings: need Stock and Units columns, got {header}")

    out = []
    for raw in rows:
        if not raw or all(not c.strip() for c in raw):
            continue
        name = raw[stock_i].strip() if stock_i < len(raw) else ""
        if not name or name.lower() in ("total", "cash"):
            continue
        qty = _parse_money(raw[units_i] if units_i < len(raw) else "")
        if qty is None:
            continue
        value = _parse_money(raw[value_i] if value_i is not None and value_i < len(raw) else "")
        cost = _parse_money(raw[cost_i] if cost_i is not None and cost_i < len(raw) else "")
        price_p = raw[price_i] if price_i is not None and price_i < len(raw) else ""
        avg = _parse_price_pence(price_p) if price_p else None
        symbol = _symbol_from_name(name)
        out.append({
            "symbol": symbol,
            "name": name,
            "quantity": qty,
            "value_gbp": value or 0.0,
            "avg_cost": avg or (cost / qty if cost and qty else None),
        })
    return out


def parse_hl_transactions(content: bytes) -> list[dict]:
    header, rows = _read_csv_rows(content)
    return _parse_transactions(header, rows, HL_TXN_DATE, HL_TXN_DESC, HL_TXN_AMOUNT, HL_TXN_IN, HL_TXN_OUT)


# --- Nutmeg parsers ---------------------------------------------------------

def parse_nutmeg_holdings(content: bytes) -> list[dict]:
    header, rows = _read_csv_rows(content)
    name_i = _find(header, NUTMEG_SYMBOL_KEYS + NUTMEG_PORTFOLIO_KEYS)
    value_i = _find(header, NUTMEG_VALUE_KEYS)
    units_i = _find(header, HL_UNITS_KEYS + ["weight", "allocation %"])

    if name_i is None:
        raise ValueError(f"nutmeg holdings: could not find name column in {header}")

    out = []
    for raw in rows:
        if not raw or all(not c.strip() for c in raw):
            continue
        name = raw[name_i].strip() if name_i < len(raw) else ""
        if not name:
            continue
        value = _parse_money(raw[value_i] if value_i is not None and value_i < len(raw) else "") or 0.0
        qty = _parse_money(raw[units_i] if units_i is not None and units_i < len(raw) else "")
        if qty is None:
            qty = 1.0 if value else 0.0
        out.append({
            "symbol": _symbol_from_name(name),
            "name": name,
            "quantity": qty,
            "value_gbp": value,
        })
    return out


def parse_nutmeg_transactions(content: bytes) -> list[dict]:
    header, rows = _read_csv_rows(content)
    return _parse_transactions(
        header, rows, NUTMEG_TXN_DATE, NUTMEG_TXN_DESC, NUTMEG_TXN_AMOUNT, [], [],
    )


def _parse_transactions(header, rows, date_keys, desc_keys, amt_keys, in_keys, out_keys) -> list[dict]:
    d_i = _find(header, date_keys)
    desc_i = _find(header, desc_keys)
    amt_i = _find(header, amt_keys)
    in_i = _find(header, in_keys) if in_keys else None
    out_i = _find(header, out_keys) if out_keys else None

    if d_i is None:
        raise ValueError(f"transactions: no date column in {header}")

    out, seen = [], {}
    for raw in rows:
        if not raw or all(not c.strip() for c in raw):
            continue
        date = _parse_date(raw[d_i]) if d_i < len(raw) else None
        if not date:
            continue
        desc = raw[desc_i].strip() if desc_i is not None and desc_i < len(raw) else "transaction"
        if amt_i is not None:
            amount = _parse_money(raw[amt_i] if amt_i < len(raw) else "")
        else:
            mi = _parse_money(raw[in_i] if in_i is not None and in_i < len(raw) else "") or 0.0
            mo = _parse_money(raw[out_i] if out_i is not None and out_i < len(raw) else "") or 0.0
            amount = mi - abs(mo)
        if amount is None:
            continue
        key = f"{date}|{amount:.2f}|{desc}"
        seen[key] = seen.get(key, 0) + 1
        ext = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
        out.append({"date": date, "description": desc[:500],
                    "amount": round(amount, 2), "external_id": ext})
    return out


def _symbol_from_name(name: str) -> str:
    m = re.search(r"\(([A-Z0-9]{2,10})\)", name)
    if m:
        return m.group(1)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip())[:32].upper()
    return slug or "UNKNOWN"


# --- unified entry ----------------------------------------------------------

def parse(content: bytes, filename: str | None = None) -> dict:
    fmt = detect_format(content, filename)
    warnings: list[str] = []
    rows: list[dict] = []
    kind = "holdings"

    try:
        if fmt == "hl_holdings":
            rows = parse_hl_holdings(content)
            kind = "holdings"
        elif fmt == "hl_transactions":
            rows = parse_hl_transactions(content)
            kind = "transactions"
        elif fmt == "nutmeg_holdings":
            rows = parse_nutmeg_holdings(content)
            kind = "holdings"
        elif fmt == "nutmeg_transactions":
            rows = parse_nutmeg_transactions(content)
            kind = "transactions"
        elif fmt == "generic":
            warnings.append("unrecognised format — try renaming file or check headers")
        else:
            warnings.append(f"unknown format {fmt}")
    except ValueError as e:
        warnings.append(str(e))

    return {"format": fmt, "kind": kind, "rows": rows, "warnings": warnings[:20]}


def apply_holdings(conn, account_id, rows) -> int:
    """Upsert instruments + holdings; write today's valuation = sum(value_gbp)."""
    n = 0
    total = 0.0
    for row in rows:
        symbol = row["symbol"]
        name = row.get("name")
        qty = float(row["quantity"])
        value = float(row.get("value_gbp") or 0)
        avg_cost = row.get("avg_cost")
        kind = row.get("kind", "fund")

        inst = conn.execute(
            """INSERT INTO instrument (symbol, name, kind, currency)
               VALUES (%s, %s, %s, 'GBP')
               ON CONFLICT (symbol, kind) DO UPDATE
                 SET name = COALESCE(EXCLUDED.name, instrument.name)
               RETURNING id""",
            (symbol, name, kind),
        ).fetchone()

        conn.execute(
            """INSERT INTO holding (account_id, instrument_id, quantity, avg_cost, as_of)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (account_id, instrument_id) DO UPDATE
                 SET quantity = EXCLUDED.quantity,
                     avg_cost = COALESCE(EXCLUDED.avg_cost, holding.avg_cost),
                     as_of = now()""",
            (account_id, inst["id"], qty, avg_cost),
        )
        n += 1
        total += value

    conn.execute(
        """INSERT INTO valuation (account_id, as_of, value_gbp, note)
           VALUES (%s, CURRENT_DATE, %s, 'imported holdings')
           ON CONFLICT (account_id, as_of) DO UPDATE SET value_gbp = EXCLUDED.value_gbp""",
        (account_id, round(total, 2)),
    )
    return n
