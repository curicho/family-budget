"""Worker: runs scheduled jobs defined by SCHEDULES_JSON (from Helm values).

Each schedule key maps to a job function here. Failure isolation is real —
one job exception never stops the scheduler loop.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

from croniter import croniter


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _db():
    import psycopg
    import psycopg.rows
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


# --- jobs -------------------------------------------------------------------

def sync_trading212() -> None:
    from app import trading212
    trading212.sync_all(log=_log)


def sync_coinbase() -> None:
    from app import coinbase
    coinbase.sync_all(log=_log)


def sync_banking() -> None:
    from app import enablebanking
    enablebanking.sync_all(log=_log)


def prices_fx() -> None:
    """Refresh prices for held instruments and write a FX GBP rate for USD if needed.
    Uses Yahoo chart endpoints (no key). Best-effort; never fails the worker."""
    import httpx

    with _db() as conn:
        instruments = conn.execute(
            """SELECT DISTINCT i.id, i.symbol, i.kind, i.currency
               FROM instrument i JOIN holding h ON h.instrument_id = i.id
               WHERE h.quantity <> 0"""
        ).fetchall()

    if not instruments:
        return _log("prices/fx: no holdings, skipping")

    updated = 0
    with httpx.Client(timeout=20) as client, _db() as conn:
        for inst in instruments:
            symbol = inst["symbol"]
            yahoo = symbol
            if inst["kind"] == "crypto":
                yahoo = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol
            try:
                r = client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}",
                    params={"interval": "1d", "range": "5d"},
                )
                if r.status_code != 200:
                    _log(f"prices/fx: {symbol} → HTTP {r.status_code}")
                    continue
                result = (r.json().get("chart") or {}).get("result") or []
                if not result:
                    continue
                meta = result[0].get("meta") or {}
                price = meta.get("regularMarketPrice") or meta.get("previousClose")
                if price is None:
                    continue
                conn.execute(
                    """INSERT INTO price (instrument_id, price_date, price)
                       VALUES (%s, CURRENT_DATE, %s)
                       ON CONFLICT (instrument_id, price_date) DO UPDATE SET price = EXCLUDED.price""",
                    (inst["id"], float(price)),
                )
                updated += 1
            except Exception as e:
                _log(f"prices/fx: {symbol} failed: {e}")

        # USD→GBP via frankfurter.app (ECB)
        try:
            r = client.get("https://api.frankfurter.app/latest", params={"from": "USD", "to": "GBP"})
            if r.status_code == 200:
                rate = float(r.json()["rates"]["GBP"])
                conn.execute(
                    """INSERT INTO fx_rate (ccy, rate_date, gbp_rate)
                       VALUES ('USD', CURRENT_DATE, %s)
                       ON CONFLICT (ccy, rate_date) DO UPDATE SET gbp_rate = EXCLUDED.gbp_rate""",
                    (rate,),
                )
                _log(f"prices/fx: USDGBP={rate}")
        except Exception as e:
            _log(f"prices/fx: FX fetch failed: {e}")

        conn.commit()
    _log(f"prices/fx: updated {updated}/{len(instruments)} instruments")


def snapshot() -> None:
    """Nightly: write per-account GBP values into snapshot from latest valuations,
    falling back to holdings×latest price for accounts without a valuation today."""
    with _db() as conn:
        conn.execute(
            """INSERT INTO snapshot (snap_date, account_id, value_gbp)
               SELECT CURRENT_DATE, v.account_id,
                      v.value_gbp * CASE WHEN a.is_liability THEN -1 ELSE 1 END
               FROM (SELECT DISTINCT ON (account_id) account_id, value_gbp
                     FROM valuation ORDER BY account_id, as_of DESC) v
               JOIN account a ON a.id = v.account_id
               ON CONFLICT (snap_date, account_id) DO UPDATE
                 SET value_gbp = EXCLUDED.value_gbp"""
        )
        # holdings-based fallback for accounts with no valuation row
        conn.execute(
            """INSERT INTO snapshot (snap_date, account_id, value_gbp)
               SELECT CURRENT_DATE, h.account_id,
                      SUM(h.quantity * COALESCE(p.price, 0)
                          * CASE WHEN i.currency = 'GBP' THEN 1
                                 ELSE COALESCE(fx.gbp_rate, 1) END)
               FROM holding h
               JOIN instrument i ON i.id = h.instrument_id
               LEFT JOIN LATERAL (
                   SELECT price FROM price
                   WHERE instrument_id = i.id ORDER BY price_date DESC LIMIT 1
               ) p ON TRUE
               LEFT JOIN LATERAL (
                   SELECT gbp_rate FROM fx_rate
                   WHERE ccy = i.currency ORDER BY rate_date DESC LIMIT 1
               ) fx ON TRUE
               WHERE NOT EXISTS (
                   SELECT 1 FROM valuation v WHERE v.account_id = h.account_id
               )
               GROUP BY h.account_id
               ON CONFLICT (snap_date, account_id) DO NOTHING"""
        )
        conn.commit()
    _log("snapshot: written")


def insights_rules() -> None:
    from app import categorise, insights, recurring

    with _db() as conn:
        n_cat = categorise.apply_rules(conn)
        n_rec = recurring.detect(conn)
        n_ins = insights.run_rules(conn)
    _log(f"insights (rules): categorised={n_cat} recurring={n_rec} insights={n_ins}")


def insights_llm() -> None:
    from app import insights

    with _db() as conn:
        n = insights.run_llm(conn)
    if n:
        _log("insights (llm): weekly review written")
    else:
        _log("insights (llm): skipped (no ANTHROPIC_API_KEY or empty)")


JOBS = {
    "sync_trading212": sync_trading212,
    "sync_coinbase": sync_coinbase,
    "sync_banking": sync_banking,
    "prices_fx": prices_fx,
    "snapshot": snapshot,
    "insights_rules": insights_rules,
    "insights_llm": insights_llm,
}


def run() -> None:
    schedules: dict[str, str] = json.loads(os.getenv("SCHEDULES_JSON", "{}"))
    now = datetime.now(timezone.utc)
    next_run = {
        name: croniter(expr, now).get_next(datetime)
        for name, expr in schedules.items()
        if name in JOBS
    }
    _log(f"worker up; schedules: {schedules}")

    while True:
        now = datetime.now(timezone.utc)
        for name, due in list(next_run.items()):
            if due <= now:
                _log(f"running {name}")
                try:
                    JOBS[name]()
                except Exception:
                    _log(f"{name} FAILED:\n{traceback.format_exc()}")
                next_run[name] = croniter(schedules[name], now).get_next(datetime)
        time.sleep(20)
