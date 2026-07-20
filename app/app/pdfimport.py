"""PDF bank-statement importer.

Works on text-based statement PDFs (not scans). Strategy:
  1. pdfplumber extracts every word with its x/y position
  2. locate the column header row ("Paid out"/"Paid in", "Money out"/"Money in",
     "Debit"/"Credit", or a single "Amount") and record each column's x-range
  3. walk subsequent lines: a line starting with a date opens a transaction;
     amounts are classified by which column's x-range they fall under;
     non-amount text between date and amounts is the description;
     continuation lines (no date) extend the previous description
Statements list transactions chronologically with running balance — the balance
column is identified and ignored.
"""
import hashlib
import io
import re
from datetime import datetime

import pdfplumber

DATE_RES = [
    (re.compile(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})$"), None),  # handled with following month word
]
MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
AMOUNT_RE = re.compile(r"^-?[£$]?\d{1,3}(,\d{3})*\.\d{2}-?$|^\(\d{1,3}(,\d{3})*\.\d{2}\)$")

OUT_HEADERS = ["paid out", "money out", "debit", "withdrawals", "out", "payments"]
IN_HEADERS = ["paid in", "money in", "credit", "deposits", "in", "receipts"]
BAL_HEADERS = ["balance"]
AMT_HEADERS = ["amount"]
DATE_HEADERS = ["date"]


def _money(s: str) -> float | None:
    s = s.strip().replace("£", "").replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _try_date(tokens: list, year_hint: int | None) -> tuple[str | None, int]:
    """Try to read a date from the start of a token list.
    Returns (iso_date | None, tokens_consumed)."""
    if not tokens:
        return None, 0
    t0 = tokens[0]["text"]
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})$", t0)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        try:
            return datetime(y, mo, d).date().isoformat(), 1
        except ValueError:
            return None, 0
    # "12 Jan" / "12 Jan 26" / "12 Jan 2026"
    if re.match(r"^\d{1,2}$", t0) and len(tokens) >= 2:
        mo = MONTHS.get(tokens[1]["text"].strip(".").lower())
        if mo:
            consumed = 2
            y = year_hint
            if len(tokens) >= 3 and re.match(r"^\d{2,4}$", tokens[2]["text"]):
                y = int(tokens[2]["text"])
                y = y + 2000 if y < 100 else y
                consumed = 3
            if y:
                try:
                    return datetime(y, mo, int(t0)).date().isoformat(), consumed
                except ValueError:
                    return None, 0
    return None, 0


def parse(content: bytes) -> dict:
    rows, warnings = [], []
    columns = None            # {'out': (x0,x1), 'in': ..., 'amount': ..., 'balance': ...}
    year_hint = None
    seen: dict[str, int] = {}
    pending = None            # transaction being assembled

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        # year hint from anywhere in the document (statement period line)
        first_text = (pdf.pages[0].extract_text() or "")
        ym = re.search(r"\b(20\d{2})\b", first_text)
        year_hint = int(ym[1]) if ym else datetime.now().year

        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False)
            # group words into lines by y position
            lines: dict[int, list] = {}
            for w in words:
                key = round(w["top"] / 3)
                lines.setdefault(key, []).append(w)
            for key in sorted(lines):
                toks = sorted(lines[key], key=lambda w: w["x0"])
                joined = " ".join(t["text"] for t in toks).lower()

                # header row?
                if any(h in joined for h in DATE_HEADERS) and (
                        any(h in joined for h in OUT_HEADERS + IN_HEADERS + AMT_HEADERS)):
                    columns = {}
                    for t in toks:
                        tl = t["text"].lower()
                        span = (t["x0"] - 15, t["x1"] + 40)
                        if tl in ("out", "debit", "withdrawals", "payments") or tl == "paid" and False:
                            columns["out"] = span
                        if tl in ("in", "credit", "deposits", "receipts"):
                            columns["in"] = span
                        if tl == "amount":
                            columns["amount"] = span
                        if tl == "balance":
                            columns["balance"] = span
                    # two-word headers ("Paid out"): find the pair
                    for i in range(len(toks) - 1):
                        pair = f"{toks[i]['text']} {toks[i+1]['text']}".lower()
                        span = (toks[i]["x0"] - 15, toks[i + 1]["x1"] + 40)
                        if pair in OUT_HEADERS:
                            columns["out"] = span
                        if pair in IN_HEADERS:
                            columns["in"] = span
                    continue

                date, consumed = _try_date(toks, year_hint)
                amounts = [(t, _money(t["text"])) for t in toks
                           if AMOUNT_RE.match(t["text"])]
                amounts = [(t, v) for t, v in amounts if v is not None]

                if date:
                    if pending:
                        rows.append(pending)
                    pending = {"date": date, "desc_parts": [], "out": None, "in": None,
                               "amount": None}
                    body = toks[consumed:]
                elif pending:
                    body = toks
                else:
                    continue

                for t in body:
                    val = _money(t["text"]) if AMOUNT_RE.match(t["text"]) else None
                    if val is None:
                        pending["desc_parts"].append(t["text"])
                        continue
                    cx = (t["x0"] + t["x1"]) / 2
                    col = None
                    if columns:
                        for name, (x0, x1) in columns.items():
                            if x0 <= cx <= x1:
                                col = name
                                break
                    if col == "balance":
                        continue
                    if col == "out":
                        pending["out"] = val
                    elif col == "in":
                        pending["in"] = val
                    elif col == "amount" or col is None:
                        # no column info: first amount wins, negative assumed spend
                        if pending["amount"] is None:
                            pending["amount"] = val
        if pending:
            rows.append(pending)

    out = []
    for p in rows:
        if p["out"] is not None:
            amount = -abs(p["out"])
        elif p["in"] is not None:
            amount = abs(p["in"])
        elif p["amount"] is not None:
            amount = p["amount"]
        else:
            continue  # descriptive line with no money (e.g. "BALANCE BROUGHT FORWARD")
        desc = re.sub(r"\s+", " ", " ".join(p["desc_parts"])).strip() or "transaction"
        if re.search(r"balance (brought|carried) forward", desc, re.I):
            continue
        key = f"{p['date']}|{amount:.2f}|{desc}"
        seen[key] = seen.get(key, 0) + 1
        ext = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
        out.append({"date": p["date"], "description": desc[:500],
                    "amount": round(amount, 2), "external_id": ext})

    if not out:
        raise ValueError(
            "no transactions found — if this is a scanned/image statement, "
            "text extraction can't read it; a CSV export is the reliable route")
    return {"rows": out, "warnings": warnings[:20]}
