"""Trading 212 Public API sync (live.trading212.com).

Auth: HTTP Basic with API_KEY:API_SECRET (preferred). T212 also accepts a legacy
``Authorization: <apiKey>`` header on some accounts — we try Basic first, then
the raw key if only TRADING212_API_KEY is set.

Docs: https://docs.trading212.com/api
"""
import base64
import os
import traceback

import httpx
import psycopg
import psycopg.rows

API = "https://live.trading212.com/api/v0"


def _db() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


def configured() -> bool:
    return bool(os.getenv("TRADING212_API_KEY"))


def _auth_headers() -> dict[str, str]:
    key = os.environ["TRADING212_API_KEY"]
    secret = os.getenv("TRADING212_API_SECRET", "")
    if secret:
        creds = base64.b64encode(f"{key}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    # legacy single-key header (some integrations use this)
    return {"Authorization": key}


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, headers=_auth_headers(), timeout=30)


def _get_json(c: httpx.Client, paths: list[str], log) -> dict | list | None:
    """Try several endpoint paths; return first 2xx JSON body or None."""
    last_err = None
    for path in paths:
        try:
            r = c.get(path)
            if r.status_code in (401, 403):
                log(f"trading212: auth failed ({r.status_code}) on {path} — "
                    "check TRADING212_API_KEY and TRADING212_API_SECRET (Basic auth)")
                return None
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            last_err = e
            log(f"trading212: {path} → HTTP {e.response.status_code}")
        except Exception as e:
            last_err = e
            log(f"trading212: {path} failed: {e}")
    if last_err:
        log(f"trading212: all paths failed for {paths[0].split('/')[2:]}: {last_err}")
    return None


def _money(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, dict):
        return float(v.get("value") or v.get("amount") or 0)
    return float(v)


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _instrument_kind(ticker: str) -> str:
    t = (ticker or "").upper()
    if "ETF" in t or t.endswith("_ETF"):
        return "etf"
    return "equity"


def _ensure_connection(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM provider_connection WHERE kind = 'trading212' LIMIT 1"
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """INSERT INTO provider_connection (kind, display_name, last_sync_status)
           VALUES ('trading212', 'Trading 212', 'new') RETURNING *"""
    ).fetchone()


def _ensure_account(conn, pc_id: str, currency: str) -> dict:
    row = conn.execute(
        """SELECT * FROM account
           WHERE connection_id = %s AND NOT archived LIMIT 1""",
        (pc_id,),
    ).fetchone()
    if row:
        return row
    hh = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
    if not hh:
        raise RuntimeError("no household row — run seed/migrations first")
    acct_type = "gia"  # API is Invest / Stocks ISA only; default GIA
    row = conn.execute(
        """INSERT INTO account (household_id, name, type, currency, connection_id,
                                valuation_stale_after)
           VALUES (%s, 'Trading 212', %s, %s, %s, INTERVAL '35 days') RETURNING *""",
        (hh["id"], acct_type, currency, pc_id),
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
        return log("trading212: not configured, skipping")
    try:
        _sync(log)
        with _db() as conn:
            conn.execute(
                """UPDATE provider_connection SET last_sync_at = now(), last_sync_status = 'ok'
                   WHERE kind = 'trading212'"""
            )
            conn.commit()
    except Exception as e:
        log(f"trading212: sync failed: {e}")
        log(traceback.format_exc().splitlines()[-1])
        with _db() as conn:
            conn.execute(
                """UPDATE provider_connection SET last_sync_status = %s
                   WHERE kind = 'trading212'""",
                (str(e)[:300],),
            )
            conn.commit()


def _sync(log) -> None:
    with _client() as c:
        summary = _get_json(c, [
            "/equity/account/summary",
            "/equity/account/cash",
            "/equity/account/metadata",
        ], log)
        if summary is None:
            raise RuntimeError("could not fetch account data (auth or endpoint error)")

        currency = _pick(summary, "currency", "accountCurrency", default="GBP")
        if isinstance(currency, dict):
            currency = currency.get("code", "GBP")
        currency = str(currency)[:3].upper()

        cash = _money(_pick(summary, "cash", "free", "available", "cashAvailable"))
        if not cash:
            cash = _money(_pick(summary, "totalCash", "cashBalance"))

        positions_raw = _get_json(c, [
            "/equity/positions",
            "/equity/portfolio",
        ], log)
        positions: list[dict] = []
        if isinstance(positions_raw, list):
            positions = positions_raw
        elif isinstance(positions_raw, dict):
            positions = (
                positions_raw.get("positions")
                or positions_raw.get("items")
                or positions_raw.get("portfolio")
                or []
            )

        if currency != "GBP":
            log(f"trading212: account currency is {currency} — valuation stored as-is (not converted)")

    with _db() as conn:
        pc = _ensure_connection(conn)
        acc = _ensure_account(conn, pc["id"], currency)
        account_id = acc["id"]

        holdings_value = 0.0
        for pos in positions:
            ticker = _pick(pos, "ticker", "instrumentCode", "symbol")
            if not ticker:
                log(f"trading212: skipping position without ticker: {pos}")
                continue
            qty = float(_pick(pos, "quantity", "qty", default=0) or 0)
            if qty == 0:
                continue
            avg = _pick(pos, "averagePrice", "averagePricePaid", "avgPrice")
            cur = _pick(pos, "currentPrice", "price", "lastPrice")
            if cur is not None:
                holdings_value += abs(qty) * float(cur)

            kind = _instrument_kind(str(ticker))
            symbol = str(ticker).split("_")[0] if "_" in str(ticker) else str(ticker)
            name = _pick(pos, "name", "instrumentName")

            inst = conn.execute(
                """INSERT INTO instrument (symbol, name, kind, currency)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (symbol, kind) DO UPDATE
                     SET name = COALESCE(EXCLUDED.name, instrument.name)
                   RETURNING id""",
                (symbol, name, kind, currency),
            ).fetchone()

            conn.execute(
                """INSERT INTO holding (account_id, instrument_id, quantity, avg_cost, as_of)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (account_id, instrument_id) DO UPDATE
                     SET quantity = EXCLUDED.quantity,
                         avg_cost = COALESCE(EXCLUDED.avg_cost, holding.avg_cost),
                         as_of = now()""",
                (account_id, inst["id"], qty, float(avg) if avg is not None else None),
            )

        total = cash + holdings_value
        note = "trading212 sync"
        if currency != "GBP":
            note = f"trading212 sync ({currency}, not converted to GBP)"
        conn.execute(
            """INSERT INTO valuation (account_id, as_of, value_gbp, note)
               VALUES (%s, CURRENT_DATE, %s, %s)
               ON CONFLICT (account_id, as_of) DO UPDATE
                 SET value_gbp = EXCLUDED.value_gbp, note = EXCLUDED.note""",
            (account_id, round(total, 2), note),
        )
        conn.commit()
        log(f"trading212: {len(positions)} positions, cash={cash:.2f}, "
            f"total={total:.2f} {currency}")
