"""Worker: runs scheduled jobs defined by SCHEDULES_JSON (from Helm values).

Each schedule key maps to a job function here. Provider syncs are stubs that
log and no-op until credentials/logic land in phase 2/3 — the scheduler,
logging and failure-isolation plumbing are real.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

from croniter import croniter


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


# --- jobs -------------------------------------------------------------------

def sync_trading212() -> None:
    if not os.getenv("TRADING212_API_KEY"):
        return _log("trading212: no API key configured, skipping")
    _log("trading212: sync not yet implemented (phase 3)")


def sync_coinbase() -> None:
    if not os.getenv("COINBASE_API_KEY"):
        return _log("coinbase: no API key configured, skipping")
    _log("coinbase: sync not yet implemented (phase 3)")


def sync_banking() -> None:
    from app import enablebanking
    enablebanking.sync_all(log=_log)


def prices_fx() -> None:
    _log("prices/fx: not yet implemented (phase 3)")


def snapshot() -> None:
    """Nightly: write per-account GBP values into snapshot (from latest valuations
    for manual accounts; holdings×price for investment accounts once phase 3 lands)."""
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
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
        conn.commit()
    _log("snapshot: written")


def insights_rules() -> None:
    _log("insights (rules): not yet implemented (phase 4)")


def insights_llm() -> None:
    _log("insights (llm): not yet implemented (phase 4)")


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
