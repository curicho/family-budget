"""Detect recurring/subscription payments from the transaction ledger."""
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

_DIGIT_RE = re.compile(r"\d+")


def _normalize_merchant(merchant: str | None, description: str) -> str:
    raw = (merchant or "").strip() or (description or "")[:40]
    s = _DIGIT_RE.sub("", raw.upper())
    return re.sub(r"\s+", " ", s).strip() or "UNKNOWN"


def _amounts_similar(amounts: list[float], tol: float = 0.08) -> bool:
    if not amounts:
        return False
    ref = sum(abs(a) for a in amounts) / len(amounts)
    if ref == 0:
        return all(a == 0 for a in amounts)
    return all(abs(abs(a) - ref) / ref <= tol for a in amounts)


def _cadence_days(dates: list[date]) -> int | None:
    if len(dates) < 2:
        return None
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if all(25 <= g <= 35 for g in gaps):
        return round(sum(gaps) / len(gaps))
    if all(6 <= g <= 8 for g in gaps):
        return round(sum(gaps) / len(gaps))
    monthly = [g for g in gaps if 25 <= g <= 35]
    weekly = [g for g in gaps if 6 <= g <= 8]
    if len(monthly) >= max(2, len(gaps) * 2 // 3):
        return round(sum(monthly) / len(monthly))
    if len(weekly) >= max(2, len(gaps) * 2 // 3):
        return round(sum(weekly) / len(weekly))
    return None


def _creep_pct(current: Decimal, history: list) -> float | None:
    if not history:
        return None
    first = history[0].get("amount") if isinstance(history[0], dict) else history[0]
    first = Decimal(str(first))
    cur = Decimal(str(current))
    if first <= 0 or cur <= first * Decimal("1.05"):
        return None
    return float((cur - first) / first * 100)


def detect(conn) -> int:
    rows = conn.execute(
        """SELECT account_id, posted_at, amount, description, merchant
           FROM transaction
           WHERE amount < 0
           ORDER BY account_id, posted_at"""
    ).fetchall()

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        key = (_normalize_merchant(r["merchant"], r["description"]), str(r["account_id"]))
        groups[key].append(r)

    upserted = 0
    for (merchant, account_id), txns in groups.items():
        if len(txns) < 3:
            continue
        txns.sort(key=lambda t: t["posted_at"])
        amounts = [float(t["amount"]) for t in txns]
        if not _amounts_similar(amounts):
            continue
        dates = [t["posted_at"] for t in txns]
        cadence = _cadence_days(dates)
        if cadence is None:
            continue

        history = [{"date": t["posted_at"].isoformat(), "amount": round(abs(float(t["amount"])), 2)}
                   for t in txns]
        current = round(abs(amounts[-1]), 2)

        conn.execute(
            """INSERT INTO recurring_payment
                 (merchant, account_id, cadence_days, current_amount,
                  first_seen, last_seen, amount_history)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (merchant, account_id) DO UPDATE SET
                 cadence_days = EXCLUDED.cadence_days,
                 current_amount = EXCLUDED.current_amount,
                 first_seen = EXCLUDED.first_seen,
                 last_seen = EXCLUDED.last_seen,
                 amount_history = EXCLUDED.amount_history""",
            (merchant, account_id, cadence, current, dates[0], dates[-1], history),
        )
        upserted += 1

    conn.commit()
    return upserted


def list_recurring(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, merchant, account_id, cadence_days, current_amount,
                  first_seen, last_seen, amount_history
           FROM recurring_payment
           ORDER BY current_amount DESC, merchant"""
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        history = r["amount_history"] or []
        creep = _creep_pct(r["current_amount"], history)
        if creep is not None:
            item["creep_pct"] = round(creep, 1)
        out.append(item)
    return out
