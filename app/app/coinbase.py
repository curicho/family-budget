"""Coinbase Advanced Trade / CDP API sync.

Env: COINBASE_API_KEY (organizations/.../apiKeys/...), COINBASE_API_SECRET (PEM),
     optional COINBASE_API_PASSPHRASE (legacy Exchange keys — unused for CDP JWT).

JWT auth per CDP docs: iss=cdp, sub=key name, kid=key name, uri=METHOD host/path.
Supports ES256 (EC PEM) and EdDSA (Ed25519 PKCS8) when cryptography is available
via pyjwt[crypto].
"""
import os
import secrets
import time
import traceback

import httpx
import jwt
import psycopg
import psycopg.rows

API = "https://api.coinbase.com"


def _db() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


def configured() -> bool:
    return bool(os.getenv("COINBASE_API_KEY") and os.getenv("COINBASE_API_SECRET"))


def _load_private_key(secret: str):
    """Load PEM private key; returns (key, algorithm) or raises with guidance."""
    pem = secret.replace("\\n", "\n").strip()
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        raise RuntimeError(
            "coinbase: cryptography not installed — pyjwt[crypto] provides it; "
            "ensure COINBASE_API_SECRET is a PEM EC (ES256) or Ed25519 key"
        ) from None
    key = load_pem_private_key(pem.encode(), password=None)
    if "EC PRIVATE KEY" in pem or "BEGIN PRIVATE KEY" in pem:
        # Ed25519 PKCS8 also uses BEGIN PRIVATE KEY — detect via key type
        key_type = type(key).__name__
        if "Ed25519" in key_type:
            return key, "EdDSA"
        return key, "ES256"
    raise RuntimeError(
        "coinbase: COINBASE_API_SECRET must be PEM (-----BEGIN EC PRIVATE KEY----- "
        "or Ed25519 -----BEGIN PRIVATE KEY-----)"
    )


def _build_jwt(method: str, path: str) -> str:
    key_name = os.environ["COINBASE_API_KEY"]
    secret = os.environ["COINBASE_API_SECRET"]
    private_key, algorithm = _load_private_key(secret)
    uri = f"{method} {API.replace('https://', '')}{path}"
    now = int(time.time())
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm=algorithm,
        headers={"kid": key_name, "nonce": secrets.token_hex(16)},
    )


def _client(method: str, path: str) -> httpx.Client:
    token = _build_jwt(method, path)
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def _require(obj: dict, field: str, ctx: str, log) -> bool:
    if field not in obj:
        log(f"coinbase: field {field!r} missing — expected in {ctx}")
        return False
    return True


def _balance_amount(acct: dict, log) -> float:
    """Extract available balance from a brokerage account object."""
    for key in ("available_balance", "balance", "hold"):
        bal = acct.get(key)
        if bal is None:
            continue
        if isinstance(bal, dict):
            if not _require(bal, "value", f"account[{key}]", log):
                return 0.0
            return float(bal["value"])
        return float(bal)
    log(f"coinbase: no balance field on account {acct.get('uuid') or acct.get('name')}")
    return 0.0


def _currency(acct: dict) -> str:
    for key in ("currency", "available_balance", "balance"):
        v = acct.get(key)
        if isinstance(v, dict) and v.get("currency"):
            return str(v["currency"])[:3].upper()
        if key == "currency" and v:
            return str(v)[:3].upper()
    return "USD"


def _ensure_connection(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM provider_connection WHERE kind = 'coinbase' LIMIT 1"
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """INSERT INTO provider_connection (kind, display_name, last_sync_status)
           VALUES ('coinbase', 'Coinbase', 'new') RETURNING *"""
    ).fetchone()


def _ensure_account(conn, pc_id: str, currency: str = "USD") -> dict:
    row = conn.execute(
        """SELECT * FROM account
           WHERE connection_id = %s AND type = 'crypto' AND NOT archived LIMIT 1""",
        (pc_id,),
    ).fetchone()
    if row:
        return row
    hh = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
    if not hh:
        raise RuntimeError("no household row")
    row = conn.execute(
        """INSERT INTO account (household_id, name, type, currency, connection_id,
                                valuation_stale_after)
           VALUES (%s, 'Coinbase', 'crypto', %s, %s, INTERVAL '7 days') RETURNING *""",
        (hh["id"], currency, pc_id),
    ).fetchone()
    member = conn.execute(
        "SELECT id FROM member WHERE NOT is_child ORDER BY created_at LIMIT 1"
    ).fetchone()
    if member:
        conn.execute(
            "INSERT INTO account_owner (account_id, member_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (row["id"], member["id"]),
        )
    return row


def sync_all(log=print) -> None:
    if not configured():
        return log("coinbase: not configured, skipping")
    try:
        _sync(log)
        with _db() as conn:
            conn.execute(
                """UPDATE provider_connection SET last_sync_at = now(), last_sync_status = 'ok'
                   WHERE kind = 'coinbase'"""
            )
            conn.commit()
    except Exception as e:
        log(f"coinbase: sync failed: {e}")
        for line in traceback.format_exc().strip().splitlines()[-3:]:
            log(line)
        with _db() as conn:
            conn.execute(
                """UPDATE provider_connection SET last_sync_status = %s
                   WHERE kind = 'coinbase'""",
                (str(e)[:300],),
            )
            conn.commit()


def _sync(log) -> None:
    path = "/api/v3/brokerage/accounts"
    try:
        with _client("GET", path) as c:
            r = c.get(path)
    except Exception as e:
        log(f"coinbase: JWT signing failed: {e}")
        log("coinbase: set COINBASE_API_KEY to your CDP key name "
            "(organizations/.../apiKeys/...) and COINBASE_API_SECRET to the PEM private key")
        return

    if r.status_code in (401, 403):
        log(f"coinbase: auth failed ({r.status_code}) — check API key permissions (view) and PEM")
        return
    r.raise_for_status()
    body = r.json()

    accounts = body.get("accounts")
    if accounts is None:
        log("coinbase: field 'accounts' missing — expected list in GET /api/v3/brokerage/accounts")
        return

    total_usd = 0.0
    active: list[tuple[dict, float, str]] = []

    for acct in accounts:
        if not _require(acct, "uuid", "accounts[]", log):
            continue
        bal = _balance_amount(acct, log)
        if bal <= 0:
            continue
        ccy = _currency(acct)
        active.append((acct, bal, ccy))
        # rough USD equivalent for valuation when no price feed
        if ccy == "USD":
            total_usd += bal
        elif ccy == "GBP":
            total_usd += bal * 1.27
        else:
            total_usd += bal  # best-effort; prices_fx job will refine later

    with _db() as conn:
        pc = _ensure_connection(conn)
        acc = _ensure_account(conn, pc["id"])
        account_id = acc["id"]

        for acct, bal, ccy in active:
            symbol = acct.get("currency") or ccy
            name = acct.get("name") or symbol
            inst = conn.execute(
                """INSERT INTO instrument (symbol, name, kind, currency)
                   VALUES (%s, %s, 'crypto', %s)
                   ON CONFLICT (symbol, kind) DO UPDATE
                     SET name = COALESCE(EXCLUDED.name, instrument.name)
                   RETURNING id""",
                (symbol, name, ccy),
            ).fetchone()
            conn.execute(
                """INSERT INTO holding (account_id, instrument_id, quantity, as_of)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (account_id, instrument_id) DO UPDATE
                     SET quantity = EXCLUDED.quantity, as_of = now()""",
                (account_id, inst["id"], bal),
            )

        note = "coinbase sync (USD approx)" if active else "coinbase sync (empty)"
        conn.execute(
            """INSERT INTO valuation (account_id, as_of, value_gbp, note)
               VALUES (%s, CURRENT_DATE, %s, %s)
               ON CONFLICT (account_id, as_of) DO UPDATE
                 SET value_gbp = EXCLUDED.value_gbp, note = EXCLUDED.note""",
            (account_id, round(total_usd / 1.27, 2) if total_usd else 0, note),
        )
        conn.commit()

    log(f"coinbase: synced {len(active)} accounts with balance, approx £{total_usd / 1.27:.2f}")
