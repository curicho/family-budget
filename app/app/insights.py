"""Rule-based and LLM-generated household insights."""
import json
import os
from datetime import date, timedelta
from decimal import Decimal

import httpx

ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


def _exists(conn, kind: str, title: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM insight WHERE kind = %s AND title = %s AND status = 'open'",
        (kind, title),
    ).fetchone())


def _insert(conn, kind: str, title: str, body: str, *,
            est_saving_gbp=None, severity: int = 3, ref=None) -> None:
    conn.execute(
        """INSERT INTO insight
             (kind, title, body, est_saving_gbp, severity, source, ref, status)
           VALUES (%s, %s, %s, %s, %s, 'rule', %s, 'open')""",
        (kind, title, body, est_saving_gbp, severity, json.dumps(ref or {})),
    )


def run_rules(conn) -> int:
    created = 0
    today = date.today()

    for r in conn.execute(
        """SELECT merchant, account_id, current_amount, cadence_days, amount_history
           FROM recurring_payment"""
    ).fetchall():
        history = r["amount_history"] or []
        if not history:
            continue
        first = Decimal(str(history[0]["amount"]))
        current = Decimal(str(r["current_amount"]))
        if first <= 0 or current < first * Decimal("1.05"):
            continue
        pct = float((current - first) / first * 100)
        title = f"Subscription creep: {r['merchant']}"
        if _exists(conn, "subscription_creep", title):
            continue
        saving = float(current - first) * 12 if float(r.get("cadence_days") or 30) >= 20 else None
        _insert(
            conn, "subscription_creep", title,
            f"{r['merchant']} rose {pct:.0f}% since first seen "
            f"(£{first:.2f} → £{current:.2f}). Review or cancel.",
            est_saving_gbp=round(saving, 2) if saving else None,
            severity=2,
            ref={"merchant": r["merchant"], "account_id": str(r["account_id"])},
        )
        created += 1

    for p in conn.execute(
        """SELECT id, kind, provider, premium, renewal_date
           FROM policy
           WHERE active AND renewal_date IS NOT NULL
             AND renewal_date <= %s AND renewal_date >= %s""",
        (today + timedelta(days=45), today),
    ).fetchall():
        days = (p["renewal_date"] - today).days
        title = f"Renewal due: {p['provider']} ({p['kind']})"
        if _exists(conn, "renewal_due", title):
            continue
        _insert(
            conn, "renewal_due", title,
            f"{p['provider']} {p['kind']} renews on {p['renewal_date']} "
            f"({days} days). Compare quotes before auto-renewal.",
            est_saving_gbp=float(p["premium"]) if p["premium"] else None,
            severity=2 if days <= 14 else 3,
            ref={"policy_id": str(p["id"]), "renewal_date": p["renewal_date"].isoformat()},
        )
        created += 1

    for a in conn.execute(
        """SELECT a.id, a.name, a.valuation_stale_after,
                  MAX(v.as_of) AS latest_valuation
           FROM account a
           LEFT JOIN valuation v ON v.account_id = a.id
           WHERE a.valuation_stale_after IS NOT NULL AND NOT a.archived
           GROUP BY a.id, a.name, a.valuation_stale_after"""
    ).fetchall():
        latest = a["latest_valuation"]
        if latest and latest >= today - a["valuation_stale_after"]:
            continue
        title = f"Stale valuation: {a['name']}"
        if _exists(conn, "valuation_stale", title):
            continue
        when = latest.isoformat() if latest else "never"
        _insert(
            conn, "valuation_stale", title,
            f"{a['name']} last valued {when}; update for accurate net worth.",
            severity=3,
            ref={"account_id": str(a["id"]), "latest_valuation": when},
        )
        created += 1

    uncategorised = conn.execute(
        "SELECT COUNT(*) AS n FROM transaction WHERE category_id IS NULL"
    ).fetchone()["n"]
    if uncategorised > 20:
        title = f"{uncategorised} uncategorised transactions"
        if not _exists(conn, "uncategorised_backlog", title):
            _insert(
                conn, "uncategorised_backlog", title,
                f"{uncategorised} transactions have no category. "
                "Categorise them for accurate spending reports.",
                severity=4,
                ref={"count": uncategorised},
            )
            created += 1

    cutoff = today - timedelta(days=60)
    for r in conn.execute(
        """SELECT merchant, account_id, current_amount, first_seen, cadence_days
           FROM recurring_payment
           WHERE first_seen >= %s""",
        (cutoff,),
    ).fetchall():
        title = f"New subscription: {r['merchant']}"
        if _exists(conn, "subscription_new", title):
            continue
        _insert(
            conn, "subscription_new", title,
            f"Detected recurring payment to {r['merchant']} "
            f"(£{r['current_amount']:.2f}, every ~{r['cadence_days']} days) "
            f"since {r['first_seen']}.",
            severity=4,
            ref={"merchant": r["merchant"], "account_id": str(r["account_id"])},
        )
        created += 1

    conn.commit()
    return created


def run_llm(conn) -> int:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return 0

    since = date.today() - timedelta(days=30)
    spend = conn.execute(
        """SELECT COALESCE(c.name, 'Uncategorised') AS category,
                  SUM(ABS(t.amount)) AS spend_gbp
           FROM transaction t
           LEFT JOIN category c ON c.id = t.category_id
           WHERE t.posted_at >= %s AND t.amount < 0
           GROUP BY c.name
           ORDER BY spend_gbp DESC""",
        (since,),
    ).fetchall()

    open_insights = conn.execute(
        """SELECT kind, title, severity FROM insight
           WHERE status = 'open' ORDER BY severity, created_at DESC LIMIT 20"""
    ).fetchall()

    spend_lines = "\n".join(
        f"- {r['category']}: £{float(r['spend_gbp']):,.2f}" for r in spend
    ) or "- (no spending recorded)"
    insight_lines = "\n".join(
        f"- [{i['kind']}] {i['title']}" for i in open_insights
    ) or "- (none)"

    prompt = f"""You are a concise UK household finance assistant. Write a short weekly review
(3-5 bullet points) based on the last 30 days of spending by category and open alerts.
Be practical and specific. Do not invent figures — only use what is provided.

Last 30 days spending:
{spend_lines}

Open alerts:
{insight_lines}"""

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()["content"][0]["text"].strip()

    title = f"Weekly review — {date.today().isoformat()}"
    conn.execute(
        """INSERT INTO insight
             (kind, title, body, severity, source, ref, status)
           VALUES ('weekly_review', %s, %s, 4, 'llm', '{}', 'open')""",
        (title, body),
    )
    conn.commit()
    return 1


def list_insights(conn, status: str = "open") -> list:
    return conn.execute(
        """SELECT id, kind, title, body, est_saving_gbp, severity, source, ref, status, created_at
           FROM insight WHERE status = %s ORDER BY severity, created_at DESC""",
        (status,),
    ).fetchall()


def set_status(conn, insight_id, status: str) -> None:
    conn.execute(
        "UPDATE insight SET status = %s WHERE id = %s",
        (status, insight_id),
    )
    conn.commit()
