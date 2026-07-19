"""Enable Banking client (api.enablebanking.com) + transaction/balance sync.

Auth: RS256 JWT signed with the application's private key; kid = application id.
Restricted production: only accounts you linked during activation are accessible.
"""
import base64
import datetime as dt
import os
import uuid

import httpx
import jwt
import psycopg
import psycopg.rows

API = "https://api.enablebanking.com"


def _token() -> str:
    app_id = os.environ["ENABLEBANKING_APP_ID"]
    key_pem = base64.b64decode(os.environ["ENABLEBANKING_PRIVATE_KEY"])
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
         "iat": now, "exp": now + 3600},
        key_pem,
        algorithm="RS256",
        headers={"kid": app_id},
    )


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )


def _db() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


def configured() -> bool:
    return bool(os.getenv("ENABLEBANKING_APP_ID") and os.getenv("ENABLEBANKING_PRIVATE_KEY"))


# ---------------------------------------------------------------------------
# Consent flow
# ---------------------------------------------------------------------------

def list_banks(country: str = "GB") -> list[dict]:
    with _client() as c:
        r = c.get("/aspsps", params={"country": country})
        r.raise_for_status()
        return [
            {"name": a["name"], "country": a["country"]}
            for a in r.json().get("aspsps", [])
        ]


def start_auth(aspsp_name: str, country: str, redirect_url: str) -> str:
    """Create an authorization and return the bank's approval URL."""
    state = uuid.uuid4().hex
    valid_until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90)).isoformat()
    with _client() as c:
        r = c.post("/auth", json={
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": "personal",
        })
        r.raise_for_status()
    with _db() as conn:
        conn.execute(
            "INSERT INTO banking_auth_state (state, aspsp_name, aspsp_country) VALUES (%s,%s,%s)",
            (state, aspsp_name, country),
        )
        conn.commit()
    return r.json()["url"]


def complete_auth(code: str, state: str) -> dict:
    """Exchange the callback code for a session; create connection + account rows."""
    with _db() as conn:
        st = conn.execute(
            "DELETE FROM banking_auth_state WHERE state = %s RETURNING *", (state,)
        ).fetchone()
        if not st:
            raise ValueError("unknown or expired state")

        with _client() as c:
            r = c.post("/sessions", json={"code": code})
            r.raise_for_status()
            sess = r.json()

        hh = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
        default_member = conn.execute(
            "SELECT id FROM member WHERE NOT is_child ORDER BY created_at LIMIT 1"
        ).fetchone()

        pc = conn.execute(
            """INSERT INTO provider_connection
                 (kind, display_name, session_id, aspsp_name, aspsp_country,
                  consent_expires_at, last_sync_status)
               VALUES ('enablebanking', %s, %s, %s, %s, now() + interval '90 days', 'new')
               RETURNING id""",
            (st["aspsp_name"], sess["session_id"], st["aspsp_name"], st["aspsp_country"]),
        ).fetchone()

        created = []
        for acc in sess.get("accounts", []):
            uid = acc if isinstance(acc, str) else acc.get("uid")
            detail = acc if isinstance(acc, dict) else {}
            name = (detail.get("name")
                    or (detail.get("account_id") or {}).get("iban")
                    or f"{st['aspsp_name']} account")
            row = conn.execute(
                """INSERT INTO account (household_id, name, type, currency, connection_id, external_ref)
                   VALUES (%s, %s, 'current', %s, %s, %s) RETURNING id, name""",
                (hh["id"], name, detail.get("currency", "GBP"), pc["id"], uid),
            ).fetchone()
            if default_member:
                conn.execute(
                    "INSERT INTO account_owner (account_id, member_id) VALUES (%s, %s)",
                    (row["id"], default_member["id"]),
                )
            created.append(row["name"])
        conn.commit()
    return {"bank": st["aspsp_name"], "accounts": created}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_all(log=print) -> None:
    if not configured():
        return log("enablebanking: not configured, skipping")
    with _db() as conn:
        conns = conn.execute(
            "SELECT * FROM provider_connection WHERE kind = 'enablebanking'"
        ).fetchall()
    for pc in conns:
        try:
            _sync_connection(pc, log)
            with _db() as conn:
                conn.execute(
                    "UPDATE provider_connection SET last_sync_at = now(), last_sync_status = 'ok' WHERE id = %s",
                    (pc["id"],),
                )
                conn.commit()
        except Exception as e:  # isolate failures per connection
            log(f"enablebanking: {pc['display_name']} sync failed: {e}")
            with _db() as conn:
                conn.execute(
                    "UPDATE provider_connection SET last_sync_status = %s WHERE id = %s",
                    (str(e)[:300], pc["id"]),
                )
                conn.commit()


def _sync_connection(pc: dict, log) -> None:
    with _db() as conn:
        accounts = conn.execute(
            "SELECT id, external_ref FROM account WHERE connection_id = %s AND NOT archived",
            (pc["id"],),
        ).fetchall()

    with _client() as c:
        for acc in accounts:
            _sync_transactions(c, acc, log)
            _sync_balance(c, acc, log)


def _sync_transactions(c: httpx.Client, acc: dict, log) -> None:
    date_from = (dt.date.today() - dt.timedelta(days=89)).isoformat()
    params: dict = {"date_from": date_from}
    n = 0
    while True:
        r = c.get(f"/accounts/{acc['external_ref']}/transactions", params=params)
        r.raise_for_status()
        body = r.json()
        with _db() as conn:
            for t in body.get("transactions", []):
                amt = float(t["transaction_amount"]["amount"])
                if t.get("credit_debit_indicator") == "DBIT":
                    amt = -abs(amt)
                desc = " ".join(t.get("remittance_information") or []) \
                    or t.get("creditor", {}).get("name") \
                    or t.get("debtor", {}).get("name") or "transaction"
                ext = t.get("entry_reference") or t.get("transaction_id")
                if not ext:  # last-resort stable id
                    ext = f"{t.get('booking_date')}:{amt}:{desc[:40]}"
                res = conn.execute(
                    """INSERT INTO transaction
                         (account_id, posted_at, amount, currency, description,
                          merchant, source, external_id)
                       VALUES (%s, %s, %s, %s, %s, %s, 'open_banking', %s)
                       ON CONFLICT (account_id, source, external_id) DO NOTHING""",
                    (acc["id"],
                     t.get("booking_date") or t.get("value_date"),
                     amt,
                     t["transaction_amount"].get("currency", "GBP"),
                     desc[:500],
                     (t.get("creditor", {}) or {}).get("name"),
                     ext),
                )
                n += res.rowcount
            conn.commit()
        ck = body.get("continuation_key")
        if not ck:
            break
        params = {"date_from": date_from, "continuation_key": ck}
    log(f"enablebanking: account {acc['id']}: {n} new transactions")


def _sync_balance(c: httpx.Client, acc: dict, log) -> None:
    r = c.get(f"/accounts/{acc['external_ref']}/balances")
    r.raise_for_status()
    balances = r.json().get("balances", [])
    if not balances:
        return
    preferred = next(
        (b for b in balances if b.get("balance_type") in ("CLBD", "XPCD", "ITBD")),
        balances[0],
    )
    value = float(preferred["balance_amount"]["amount"])
    with _db() as conn:
        conn.execute(
            """INSERT INTO valuation (account_id, as_of, value_gbp, note)
               VALUES (%s, CURRENT_DATE, %s, 'bank balance')
               ON CONFLICT (account_id, as_of) DO UPDATE SET value_gbp = EXCLUDED.value_gbp""",
            (acc["id"], value),
        )
        conn.commit()
