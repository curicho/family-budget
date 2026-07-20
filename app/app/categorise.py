"""Auto-categorisation via category_rule table."""
import re

_LEARNED_PRIORITY = 10
_NOISE = frozenset({
    "THE", "A", "AN", "TO", "FROM", "FOR", "AND", "OR", "PAYMENT", "PAY",
    "CARD", "DEBIT", "CREDIT", "POS", "VISA", "MASTERCARD", "CONTACTLESS",
    "LTD", "LIMITED", "PLC", "UK", "GB", "REF", "TRANSFER",
})


def _field_text(rule: dict, description: str, merchant: str | None) -> str:
    if rule["match_field"] == "merchant":
        return merchant or ""
    return description or ""


def _matches(rule: dict, description: str, merchant: str | None) -> bool:
    text = _field_text(rule, description, merchant)
    if not text:
        return False
    value = rule["match_value"]
    kind = rule["match_kind"]
    if kind == "contains":
        return value.lower() in text.lower()
    if kind == "equals":
        return text.lower() == value.lower()
    if kind == "regex":
        return re.search(value, text, re.IGNORECASE) is not None
    return False


def _load_rules(conn) -> list[dict]:
    return conn.execute(
        """SELECT priority, match_field, match_kind, match_value,
                  category_id, member_id, activity_tag
           FROM category_rule
           ORDER BY priority ASC, created_at ASC"""
    ).fetchall()


def _best_rule(rules: list[dict], description: str, merchant: str | None) -> dict | None:
    for rule in rules:
        if _matches(rule, description, merchant):
            return rule
    return None


def categorise_one(conn, description: str, merchant: str | None = None) -> dict | None:
    rule = _best_rule(_load_rules(conn), description, merchant)
    if not rule:
        return None
    return {
        "category_id": rule["category_id"],
        "member_id": rule["member_id"],
        "activity_tag": rule["activity_tag"],
    }


def apply_rules(conn, txn_ids: list | None = None) -> int:
    rules = _load_rules(conn)
    if txn_ids:
        txns = conn.execute(
            """SELECT id, description, merchant FROM transaction
               WHERE category_id IS NULL AND id = ANY(%s)""",
            (txn_ids,),
        ).fetchall()
    else:
        txns = conn.execute(
            "SELECT id, description, merchant FROM transaction WHERE category_id IS NULL"
        ).fetchall()

    updated = 0
    for txn in txns:
        rule = _best_rule(rules, txn["description"], txn["merchant"])
        if not rule:
            continue
        by = "learned" if rule["priority"] <= _LEARNED_PRIORITY else "rule"
        conn.execute(
            """UPDATE transaction
               SET category_id = %s, member_id = %s, activity_tag = %s, categorised_by = %s
               WHERE id = %s""",
            (rule["category_id"], rule["member_id"], rule["activity_tag"], by, txn["id"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def _learn_token(description: str, merchant: str | None) -> tuple[str, str] | None:
    if merchant and merchant.strip():
        token = merchant.strip().upper()
        if len(token) >= 4:
            return "merchant", token
    words = [w for w in re.findall(r"[A-Za-z0-9]+", description.upper()) if w not in _NOISE]
    for word in words:
        if len(word) >= 4:
            return "description", word
    if len(words) >= 2:
        pair = f"{words[0]} {words[1]}"
        if len(pair) >= 4:
            return "description", pair
    return None


def learn_from_user(
    conn,
    description: str,
    merchant: str | None,
    category_id: str,
    member_id: str | None,
    activity_tag: str | None,
) -> None:
    learned = _learn_token(description, merchant)
    if not learned:
        return
    match_field, match_value = learned
    existing = conn.execute(
        """SELECT id FROM category_rule
           WHERE match_field = %s AND match_kind = 'contains' AND match_value = %s""",
        (match_field, match_value),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE category_rule
               SET category_id = %s, member_id = %s, activity_tag = %s
               WHERE id = %s""",
            (category_id, member_id, activity_tag, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO category_rule
               (priority, match_field, match_kind, match_value,
                category_id, member_id, activity_tag)
               VALUES (%s, %s, 'contains', %s, %s, %s, %s)""",
            (_LEARNED_PRIORITY, match_field, match_value, category_id, member_id, activity_tag),
        )
    conn.commit()


def uncategorised_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM transaction WHERE category_id IS NULL"
    ).fetchone()
    return row["n"]


def review_queue(conn, limit: int = 50) -> list[dict]:
    return conn.execute(
        """SELECT t.id, t.posted_at, t.amount, t.currency, t.description, t.merchant,
                  t.account_id, a.name AS account_name
           FROM transaction t
           JOIN account a ON a.id = t.account_id
           WHERE t.category_id IS NULL
           ORDER BY t.posted_at DESC, t.created_at DESC
           LIMIT %s""",
        (limit,),
    ).fetchall()
