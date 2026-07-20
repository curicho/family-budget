"""API service. Phase-1 surface: health, auth, members, accounts, net worth."""
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import psycopg.rows
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

hasher = PasswordHasher()
SESSION_TTL = timedelta(hours=12)
SESSION_TTL_SECS = int(SESSION_TTL.total_seconds())
_sessions: dict[str, datetime] = {}  # fallback if Redis is down


def _redis():
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis
        return redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    except Exception:
        return None


def _session_set(token: str) -> None:
    r = _redis()
    if r is not None:
        try:
            r.setex(f"fb:sess:{token}", SESSION_TTL_SECS, "1")
            return
        except Exception:
            pass
    _sessions[token] = datetime.now(timezone.utc) + SESSION_TTL


def _session_ok(token: str) -> bool:
    if not token:
        return False
    r = _redis()
    if r is not None:
        try:
            return bool(r.exists(f"fb:sess:{token}"))
        except Exception:
            pass
    expiry = _sessions.get(token)
    return bool(expiry and expiry >= datetime.now(timezone.utc))


def _session_clear(token: str) -> None:
    if not token:
        return
    r = _redis()
    if r is not None:
        try:
            r.delete(f"fb:sess:{token}")
        except Exception:
            pass
    _sessions.pop(token, None)


def db() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Bootstrap: ensure a household exists
    with db() as conn:
        row = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
        if not row:
            conn.execute("INSERT INTO household (name) VALUES ('Home')")
            conn.commit()
    yield


app = FastAPI(title="family-budget", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


# --------------------------------------------------------------------------
# Auth (single user; TOTP/passkeys to follow — see docs/mac-mini-setup.md §5)
# --------------------------------------------------------------------------
class Credentials(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
def register(creds: Credentials):
    """First-run only: creates the single app user, then locks itself."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM app_user LIMIT 1").fetchone():
            raise HTTPException(409, "user already exists")
        conn.execute(
            "INSERT INTO app_user (email, password_hash) VALUES (%s, %s)",
            (creds.email.lower(), hasher.hash(creds.password)),
        )
        conn.commit()
    return {"ok": True}


@app.post("/api/auth/login")
def login(creds: Credentials):
    with db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM app_user WHERE email = %s", (creds.email.lower(),)
        ).fetchone()
    try:
        assert row
        hasher.verify(row["password_hash"], creds.password)
    except Exception:
        raise HTTPException(401, "invalid credentials")
    token = uuid.uuid4().hex
    _session_set(token)
    return {"token": token}


@app.post("/api/auth/logout")
def logout(request: Request):
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    _session_clear(token)
    return {"ok": True}


def authed(request: Request) -> None:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not _session_ok(token):
        raise HTTPException(401, "not authenticated")


# --------------------------------------------------------------------------
# Members & accounts
# --------------------------------------------------------------------------
class MemberIn(BaseModel):
    name: str
    is_child: bool = False
    date_of_birth: str | None = None


@app.get("/api/members", dependencies=[Depends(authed)])
def list_members():
    with db() as conn:
        return conn.execute("SELECT * FROM member ORDER BY created_at").fetchall()


@app.post("/api/members", dependencies=[Depends(authed)])
def create_member(m: MemberIn):
    with db() as conn:
        hh = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
        row = conn.execute(
            """INSERT INTO member (household_id, name, is_child, date_of_birth)
               VALUES (%s, %s, %s, %s) RETURNING *""",
            (hh["id"], m.name, m.is_child, m.date_of_birth),
        ).fetchone()
        conn.commit()
        return row


class AccountIn(BaseModel):
    name: str
    type: str
    currency: str = "GBP"
    owner_member_id: str
    is_liability: bool = False
    valuation_stale_after_days: int | None = None


@app.get("/api/accounts", dependencies=[Depends(authed)])
def list_accounts():
    with db() as conn:
        return conn.execute(
            """SELECT a.*, json_agg(json_build_object('member_id', o.member_id,
                                                      'share_pct', o.share_pct)) AS owners
               FROM account a LEFT JOIN account_owner o ON o.account_id = a.id
               WHERE NOT a.archived GROUP BY a.id ORDER BY a.created_at"""
        ).fetchall()


_LIABILITY_TYPES = {"credit_card", "liability"}
_STALE_35D_TYPES = {"sipp", "workplace_pension", "isa", "jisa", "gia"}
_STALE_90D_TYPES = {"property", "vehicle", "other_asset"}


@app.post("/api/accounts", dependencies=[Depends(authed)])
def create_account(a: AccountIn):
    is_liability = True if a.type in _LIABILITY_TYPES else a.is_liability
    stale_days = a.valuation_stale_after_days
    if stale_days is None:
        if a.type in _STALE_35D_TYPES:
            stale_days = 35
        elif a.type in _STALE_90D_TYPES:
            stale_days = 90
    with db() as conn:
        hh = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
        row = conn.execute(
            """INSERT INTO account (household_id, name, type, currency, is_liability,
                                     valuation_stale_after)
               VALUES (%s, %s, %s::account_type, %s, %s, (%s::text || ' days')::interval)
               RETURNING *""",
            (hh["id"], a.name, a.type, a.currency, is_liability, stale_days),
        ).fetchone()
        conn.execute(
            "INSERT INTO account_owner (account_id, member_id) VALUES (%s, %s)",
            (row["id"], a.owner_member_id),
        )
        conn.commit()
        return row


class ValuationIn(BaseModel):
    account_id: str
    as_of: str
    value_gbp: float
    note: str | None = None


@app.post("/api/valuations", dependencies=[Depends(authed)])
def add_valuation(v: ValuationIn):
    with db() as conn:
        conn.execute(
            """INSERT INTO valuation (account_id, as_of, value_gbp, note)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (account_id, as_of) DO UPDATE SET value_gbp = EXCLUDED.value_gbp""",
            (v.account_id, v.as_of, v.value_gbp, v.note),
        )
        conn.commit()
    return {"ok": True}


@app.get("/api/networth", dependencies=[Depends(authed)])
def networth():
    """Latest valuation per account, rolled up per member via ownership shares."""
    with db() as conn:
        rows = conn.execute(
            """WITH latest AS (
                   SELECT DISTINCT ON (account_id) account_id, value_gbp
                   FROM valuation ORDER BY account_id, as_of DESC
               )
               SELECT m.id AS member_id, m.name,
                      COALESCE(SUM(l.value_gbp * o.share_pct / 100
                                   * CASE WHEN a.is_liability THEN -1 ELSE 1 END), 0) AS net_worth_gbp
               FROM member m
               LEFT JOIN account_owner o ON o.member_id = m.id
               LEFT JOIN account a ON a.id = o.account_id AND NOT a.archived
               LEFT JOIN latest l ON l.account_id = a.id
               GROUP BY m.id, m.name ORDER BY m.name"""
        ).fetchall()
        total = sum(r["net_worth_gbp"] for r in rows)
    return {"total_gbp": total, "by_member": rows}


# --------------------------------------------------------------------------
# Banking (Enable Banking)
# --------------------------------------------------------------------------
from fastapi.responses import HTMLResponse  # noqa: E402

from app import enablebanking  # noqa: E402


def _redirect_url(request: Request) -> str:
    base = os.getenv("PUBLIC_BASE_URL")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return f"{base.rstrip('/')}/api/banking/callback"


@app.get("/api/banking/status", dependencies=[Depends(authed)])
def banking_status():
    if not enablebanking.configured():
        return {"configured": False, "connections": []}
    with db() as conn:
        conns = conn.execute(
            """SELECT id, display_name, aspsp_name, consent_expires_at,
                      last_sync_at, last_sync_status,
                      (consent_expires_at < now() + interval '14 days') AS expiring_soon
               FROM provider_connection WHERE kind = 'enablebanking'
               ORDER BY created_at"""
        ).fetchall()
    return {"configured": True, "connections": conns}


@app.get("/api/banking/banks", dependencies=[Depends(authed)])
def banking_banks(country: str = "GB"):
    return enablebanking.list_banks(country)


class ConnectIn(BaseModel):
    aspsp_name: str
    country: str = "GB"


@app.post("/api/banking/connect", dependencies=[Depends(authed)])
def banking_connect(body: ConnectIn, request: Request):
    url = enablebanking.start_auth(body.aspsp_name, body.country, _redirect_url(request))
    return {"url": url}


@app.get("/api/banking/callback")
def banking_callback(code: str | None = None, state: str | None = None,
                     error: str | None = None):
    """Unauthenticated by necessity (the bank redirects here); the one-time
    state token minted by start_auth is what authorizes this call."""
    if error or not code or not state:
        return HTMLResponse(
            f"<h3>Bank connection failed: {error or 'missing code'}</h3>"
            "<a href='/'>Back to app</a>", status_code=400)
    try:
        result = enablebanking.complete_auth(code, state)
    except Exception as e:
        return HTMLResponse(
            f"<h3>Bank connection failed: {e}</h3><a href='/'>Back to app</a>",
            status_code=400)
    accounts = ", ".join(result["accounts"]) or "no accounts returned"
    return HTMLResponse(
        f"<h3>Connected {result['bank']}</h3><p>Linked: {accounts}</p>"
        "<p>Transactions will appear after the next sync (or within a minute "
        "if you trigger one).</p><a href='/'>Back to app</a>")


@app.post("/api/banking/sync", dependencies=[Depends(authed)])
def banking_sync():
    enablebanking.sync_all()
    return {"ok": True}


@app.get("/api/transactions", dependencies=[Depends(authed)])
def list_transactions(
    limit: int = 50,
    account_id: str | None = None,
    category_id: str | None = None,
    member_id: str | None = None,
    uncategorised: bool = False,
    q: str | None = None,
):
    where = ["1=1"]
    params: list = []
    if account_id:
        where.append("t.account_id = %s")
        params.append(account_id)
    if category_id:
        where.append("t.category_id = %s")
        params.append(category_id)
    if member_id:
        where.append("t.member_id = %s")
        params.append(member_id)
    if uncategorised:
        where.append("t.category_id IS NULL")
    if q:
        where.append("(t.description ILIKE %s OR COALESCE(t.merchant, '') ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    params.append(min(limit, 200))
    with db() as conn:
        return conn.execute(
            f"""SELECT t.id, t.posted_at, t.amount, t.currency, t.description, t.merchant,
                       t.category_id, t.member_id, t.activity_tag, t.categorised_by,
                       a.name AS account_name, c.name AS category_name, m.name AS member_name
                FROM transaction t
                JOIN account a ON a.id = t.account_id
                LEFT JOIN category c ON c.id = t.category_id
                LEFT JOIN member m ON m.id = t.member_id
                WHERE {' AND '.join(where)}
                ORDER BY t.posted_at DESC, t.created_at DESC LIMIT %s""",
            params,
        ).fetchall()


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------
from fastapi import UploadFile, File, Form  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import json as _json  # noqa: E402

from psycopg.types.json import Json  # noqa: E402

from app import csvimport  # noqa: E402
from app import pdfimport  # noqa: E402
from app import categorise  # noqa: E402


def _insert_txns(conn, account_id, currency, rows, doc_id) -> tuple[int, list]:
    """Insert parsed rows as csv_import transactions; returns (inserted, new_ids)."""
    inserted, ids = 0, []
    for r in rows:
        res = conn.execute(
            """INSERT INTO transaction
                 (account_id, posted_at, amount, currency, description,
                  source, external_id, document_id)
               VALUES (%s, %s, %s, %s, %s, 'csv_import', %s, %s)
               ON CONFLICT (account_id, source, external_id) DO NOTHING
               RETURNING id""",
            (account_id, r["date"], r["amount"], currency, r["description"],
             r["external_id"], doc_id),
        ).fetchone()
        if res:
            inserted += 1
            ids.append(res["id"])
    return inserted, ids


@app.post("/api/import/csv", dependencies=[Depends(authed)])
async def import_csv(
    account_id: str = Form(...),
    dry_run: bool = Form(False),
    file: UploadFile = File(...),
):
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "file too large")
    is_pdf = content[:5] == b"%PDF-" or (file.filename or "").lower().endswith(".pdf")
    try:
        if is_pdf:
            parsed = pdfimport.parse(content)
            parsed.setdefault("mapping", {"format": "pdf"})
        else:
            parsed = csvimport.parse(content)
    except ValueError as e:
        raise HTTPException(422, str(e))

    if dry_run:
        return {"detected": parsed["mapping"], "row_count": len(parsed["rows"]),
                "preview": parsed["rows"][:5], "warnings": parsed["warnings"]}

    sha = _hashlib.sha256(content).hexdigest()
    with db() as conn:
        acc = conn.execute(
            "SELECT id, currency FROM account WHERE id = %s", (account_id,)
        ).fetchone()
        if not acc:
            raise HTTPException(404, "account not found")

        doc = conn.execute(
            """INSERT INTO document (kind, object_key, filename, sha256)
               VALUES ('csv', %s, %s, %s)
               ON CONFLICT (sha256) DO UPDATE SET filename = EXCLUDED.filename
               RETURNING id""",
            (f"csv/{sha}", file.filename or "import.csv", sha),
        ).fetchone()

        inserted, ids = _insert_txns(conn, acc["id"], acc["currency"], parsed["rows"], doc["id"])
        conn.commit()
        categorised = categorise.apply_rules(conn, ids) if ids else 0

    return {"imported": inserted, "duplicates_skipped": len(parsed["rows"]) - inserted,
            "warnings": parsed["warnings"], "detected": parsed["mapping"],
            "categorised": categorised}


# --------------------------------------------------------------------------
# Home / dashboard
# --------------------------------------------------------------------------
from app import insights  # noqa: E402

_SPEND_FILTER = "(c.kind IS NULL OR c.kind NOT IN ('transfer', 'tax', 'pension'))"


@app.get("/api/home", dependencies=[Depends(authed)])
def home():
    with db() as conn:
        nw_rows = conn.execute(
            """WITH latest AS (
                   SELECT DISTINCT ON (account_id) account_id, value_gbp
                   FROM valuation ORDER BY account_id, as_of DESC
               )
               SELECT m.id AS member_id, m.name,
                      COALESCE(SUM(l.value_gbp * o.share_pct / 100
                                   * CASE WHEN a.is_liability THEN -1 ELSE 1 END), 0) AS net_worth_gbp
               FROM member m
               LEFT JOIN account_owner o ON o.member_id = m.id
               LEFT JOIN account a ON a.id = o.account_id AND NOT a.archived
               LEFT JOIN latest l ON l.account_id = a.id
               GROUP BY m.id, m.name ORDER BY m.name"""
        ).fetchall()
        net_worth = {"total_gbp": sum(r["net_worth_gbp"] for r in nw_rows), "by_member": nw_rows}

        month_spend = conn.execute(
            f"""SELECT COALESCE(SUM(-t.amount), 0) AS v
                FROM transaction t LEFT JOIN category c ON c.id = t.category_id
                WHERE t.amount < 0
                  AND t.posted_at >= date_trunc('month', CURRENT_DATE)
                  AND t.posted_at < date_trunc('month', CURRENT_DATE) + interval '1 month'
                  AND {_SPEND_FILTER}"""
        ).fetchone()["v"]

        prior_total = conn.execute(
            f"""SELECT COALESCE(SUM(-t.amount), 0) AS v
                FROM transaction t LEFT JOIN category c ON c.id = t.category_id
                WHERE t.amount < 0
                  AND t.posted_at >= date_trunc('month', CURRENT_DATE) - interval '3 months'
                  AND t.posted_at < date_trunc('month', CURRENT_DATE)
                  AND {_SPEND_FILTER}"""
        ).fetchone()["v"]
        baseline_spend = round(float(prior_total) / 3, 2)

        this_month_cat = {
            r["category"]: float(r["v"]) for r in conn.execute(
                f"""SELECT COALESCE(c.name, 'Uncategorised') AS category, SUM(-t.amount) AS v
                    FROM transaction t LEFT JOIN category c ON c.id = t.category_id
                    WHERE t.amount < 0
                      AND t.posted_at >= date_trunc('month', CURRENT_DATE)
                      AND t.posted_at < date_trunc('month', CURRENT_DATE) + interval '1 month'
                      AND {_SPEND_FILTER}
                    GROUP BY category"""
            ).fetchall()
        }
        prior_avg_cat = {
            r["category"]: float(r["v"]) / 3 for r in conn.execute(
                f"""SELECT COALESCE(c.name, 'Uncategorised') AS category, SUM(-t.amount) AS v
                    FROM transaction t LEFT JOIN category c ON c.id = t.category_id
                    WHERE t.amount < 0
                      AND t.posted_at >= date_trunc('month', CURRENT_DATE) - interval '3 months'
                      AND t.posted_at < date_trunc('month', CURRENT_DATE)
                      AND {_SPEND_FILTER}
                    GROUP BY category"""
            ).fetchall()
        }
        movers = []
        for cat in set(this_month_cat) | set(prior_avg_cat):
            this_m = round(this_month_cat.get(cat, 0.0), 2)
            prior = round(prior_avg_cat.get(cat, 0.0), 2)
            movers.append({"category": cat, "this_month": this_m, "prior_avg": prior,
                           "delta": round(this_m - prior, 2)})
        top_movers = sorted(movers, key=lambda mv: abs(mv["delta"]), reverse=True)[:5]

        upcoming_renewals = conn.execute(
            """SELECT id, kind, provider, renewal_date, premium FROM policy
               WHERE active AND renewal_date IS NOT NULL
                 AND renewal_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 60
               ORDER BY renewal_date"""
        ).fetchall()

        uncategorised_n = categorise.uncategorised_count(conn)

        stale_rows = conn.execute(
            """SELECT a.id, a.name, a.type,
                      EXTRACT(DAY FROM a.valuation_stale_after)::int AS stale_after_days,
                      MAX(v.as_of) AS last_valuation,
                      (CURRENT_DATE - MAX(v.as_of)) AS days_since
               FROM account a LEFT JOIN valuation v ON v.account_id = a.id
               WHERE a.valuation_stale_after IS NOT NULL AND NOT a.archived
               GROUP BY a.id, a.name, a.type, a.valuation_stale_after"""
        ).fetchall()
        stale_assets = [
            r for r in stale_rows
            if r["days_since"] is None or r["days_since"] > r["stale_after_days"]
        ]

        insights_preview = insights.list_insights(conn, "open")[:5]

    return {
        "net_worth": net_worth,
        "month_spend": round(float(month_spend), 2),
        "baseline_spend": baseline_spend,
        "top_movers": top_movers,
        "upcoming_renewals": upcoming_renewals,
        "uncategorised": uncategorised_n,
        "stale_assets": stale_assets,
        "insights_preview": insights_preview,
    }


# --------------------------------------------------------------------------
# Categories, rules & categorisation review
# --------------------------------------------------------------------------
class CategoryIn(BaseModel):
    name: str
    kind: str
    parent_id: str | None = None


@app.get("/api/categories", dependencies=[Depends(authed)])
def list_categories():
    with db() as conn:
        return conn.execute(
            "SELECT id, parent_id, name, kind FROM category ORDER BY parent_id NULLS FIRST, name"
        ).fetchall()


@app.post("/api/categories", dependencies=[Depends(authed)])
def create_category(c: CategoryIn):
    with db() as conn:
        row = conn.execute(
            "INSERT INTO category (name, kind, parent_id) VALUES (%s, %s, %s) RETURNING *",
            (c.name, c.kind, c.parent_id),
        ).fetchone()
        conn.commit()
        return row


class CategoryRuleIn(BaseModel):
    match_field: str
    match_kind: str
    match_value: str
    category_id: str
    member_id: str | None = None
    activity_tag: str | None = None
    priority: int = 100


@app.get("/api/category-rules", dependencies=[Depends(authed)])
def list_category_rules():
    with db() as conn:
        return conn.execute(
            """SELECT cr.*, c.name AS category_name FROM category_rule cr
               JOIN category c ON c.id = cr.category_id
               ORDER BY cr.priority, cr.created_at"""
        ).fetchall()


@app.post("/api/category-rules", dependencies=[Depends(authed)])
def create_category_rule(r: CategoryRuleIn):
    with db() as conn:
        row = conn.execute(
            """INSERT INTO category_rule
                 (priority, match_field, match_kind, match_value, category_id, member_id, activity_tag)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (r.priority, r.match_field, r.match_kind, r.match_value,
             r.category_id, r.member_id, r.activity_tag),
        ).fetchone()
        conn.commit()
        return row


@app.delete("/api/category-rules/{rule_id}", dependencies=[Depends(authed)])
def delete_category_rule(rule_id: str):
    with db() as conn:
        res = conn.execute("DELETE FROM category_rule WHERE id = %s", (rule_id,))
        conn.commit()
        if res.rowcount == 0:
            raise HTTPException(404, "rule not found")
    return {"ok": True}


@app.post("/api/categorise/apply", dependencies=[Depends(authed)])
def categorise_apply():
    with db() as conn:
        updated = categorise.apply_rules(conn)
    return {"updated": updated}


@app.get("/api/categorise/review", dependencies=[Depends(authed)])
def categorise_review(limit: int = 50):
    with db() as conn:
        return categorise.review_queue(conn, limit)


class TransactionPatch(BaseModel):
    category_id: str | None = None
    member_id: str | None = None
    activity_tag: str | None = None


@app.patch("/api/transactions/{txn_id}", dependencies=[Depends(authed)])
def patch_transaction(txn_id: str, body: TransactionPatch):
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "no fields to update")
    with db() as conn:
        txn = conn.execute(
            "SELECT description, merchant FROM transaction WHERE id = %s", (txn_id,)
        ).fetchone()
        if not txn:
            raise HTTPException(404, "transaction not found")

        cols = list(updates.keys())
        set_clause = ", ".join(f"{c} = %s" for c in cols)
        params = [updates[c] for c in cols]
        if "category_id" in updates:
            set_clause += ", categorised_by = 'user'"
        params.append(txn_id)

        row = conn.execute(
            f"UPDATE transaction SET {set_clause} WHERE id = %s RETURNING *", params
        ).fetchone()

        if "category_id" in updates and updates["category_id"]:
            categorise.learn_from_user(
                conn, txn["description"], txn["merchant"],
                updates["category_id"], updates.get("member_id"), updates.get("activity_tag"),
            )
        conn.commit()
        return row


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------
class BudgetIn(BaseModel):
    category_id: str
    year_month: str
    amount_gbp: float


@app.get("/api/budgets", dependencies=[Depends(authed)])
def list_budgets(month: str | None = None):
    with db() as conn:
        ym = month or conn.execute("SELECT to_char(CURRENT_DATE, 'YYYY-MM') AS ym").fetchone()["ym"]
        cats = conn.execute(
            """SELECT id, name FROM category
               WHERE kind = 'expense' ORDER BY name"""
        ).fetchall()
        budget_map = {
            str(b["category_id"]): b
            for b in conn.execute(
                "SELECT id, category_id, year_month, amount_gbp FROM category_budget WHERE year_month = %s",
                (ym,),
            ).fetchall()
        }
        out = []
        for c in cats:
            b = budget_map.get(str(c["id"]))
            amount = float(b["amount_gbp"]) if b else 0.0
            spent = conn.execute(
                """SELECT COALESCE(SUM(-amount), 0) AS spent FROM transaction
                   WHERE category_id = %s AND amount < 0
                     AND to_char(posted_at, 'YYYY-MM') = %s""",
                (c["id"], ym),
            ).fetchone()["spent"]
            spent_f = float(spent)
            out.append({
                "id": b["id"] if b else None,
                "category_id": c["id"],
                "category_name": c["name"],
                "year_month": ym,
                "amount_gbp": amount,
                "spent": spent_f,
                "progress": round(spent_f / amount, 4) if amount else None,
            })
        return out


@app.put("/api/budgets", dependencies=[Depends(authed)])
def upsert_budget(b: BudgetIn):
    with db() as conn:
        row = conn.execute(
            """INSERT INTO category_budget (category_id, year_month, amount_gbp)
               VALUES (%s, %s, %s)
               ON CONFLICT (category_id, year_month) DO UPDATE SET amount_gbp = EXCLUDED.amount_gbp
               RETURNING *""",
            (b.category_id, b.year_month, b.amount_gbp),
        ).fetchone()
        conn.commit()
        return row


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------
_ASSET_TYPES = ["property", "vehicle", "sipp", "workplace_pension", "isa", "jisa",
                 "gia", "crypto", "other_asset"]


@app.get("/api/assets", dependencies=[Depends(authed)])
def list_assets():
    with db() as conn:
        rows = conn.execute(
            """WITH latest AS (
                   SELECT DISTINCT ON (account_id) account_id, as_of, value_gbp
                   FROM valuation ORDER BY account_id, as_of DESC
               )
               SELECT a.id, a.name, a.type,
                      EXTRACT(DAY FROM a.valuation_stale_after)::int AS stale_after_days,
                      l.as_of AS last_valuation,
                      l.as_of AS latest_as_of,
                      l.value_gbp AS latest_value_gbp,
                      (CURRENT_DATE - l.as_of) AS days_since,
                      COALESCE((
                        SELECT json_agg(m.name)
                        FROM account_owner o JOIN member m ON m.id = o.member_id
                        WHERE o.account_id = a.id
                      ), '[]'::json) AS owners
               FROM account a
               LEFT JOIN latest l ON l.account_id = a.id
               WHERE a.type = ANY(%s::account_type[]) AND NOT a.archived
               ORDER BY a.name""",
            (_ASSET_TYPES,),
        ).fetchall()
        out = []
        for r in rows:
            is_stale = r["stale_after_days"] is not None and (
                r["days_since"] is None or r["days_since"] > r["stale_after_days"])
            out.append({**r, "is_stale": is_stale})
        return out


# --------------------------------------------------------------------------
# Protection policies
# --------------------------------------------------------------------------
class PolicyIn(BaseModel):
    kind: str
    provider: str
    policyholder_member_id: str | None = None
    cover_amount: float | None = None
    premium: float
    premium_freq: str = "monthly"
    start_date: str | None = None
    renewal_date: str | None = None
    match_pattern: str | None = None
    active: bool = True


class PolicyPatch(BaseModel):
    kind: str | None = None
    provider: str | None = None
    policyholder_member_id: str | None = None
    cover_amount: float | None = None
    premium: float | None = None
    premium_freq: str | None = None
    start_date: str | None = None
    renewal_date: str | None = None
    match_pattern: str | None = None
    active: bool | None = None


@app.get("/api/policies", dependencies=[Depends(authed)])
def list_policies():
    with db() as conn:
        return conn.execute(
            """SELECT p.*, m.name AS policyholder_name FROM policy p
               LEFT JOIN member m ON m.id = p.policyholder_member_id
               ORDER BY p.provider"""
        ).fetchall()


@app.post("/api/policies", dependencies=[Depends(authed)])
def create_policy(p: PolicyIn):
    with db() as conn:
        row = conn.execute(
            """INSERT INTO policy (kind, provider, policyholder_member_id, cover_amount,
                                    premium, premium_freq, start_date, renewal_date,
                                    match_pattern, active)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (p.kind, p.provider, p.policyholder_member_id, p.cover_amount, p.premium,
             p.premium_freq, p.start_date, p.renewal_date, p.match_pattern, p.active),
        ).fetchone()
        conn.commit()
        return row


@app.patch("/api/policies/{policy_id}", dependencies=[Depends(authed)])
def update_policy(policy_id: str, body: PolicyPatch):
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "no fields to update")
    cols = list(updates.keys())
    set_clause = ", ".join(f"{c} = %s" for c in cols)
    params = [updates[c] for c in cols] + [policy_id]
    with db() as conn:
        row = conn.execute(
            f"UPDATE policy SET {set_clause} WHERE id = %s RETURNING *", params
        ).fetchone()
        if not row:
            raise HTTPException(404, "policy not found")
        conn.commit()
        return row


@app.get("/api/policies/renewals", dependencies=[Depends(authed)])
def policy_renewals(days: int = 60):
    with db() as conn:
        return conn.execute(
            """SELECT id, kind, provider, renewal_date, premium FROM policy
               WHERE active AND renewal_date IS NOT NULL
                 AND renewal_date BETWEEN CURRENT_DATE AND CURRENT_DATE + %s::int
               ORDER BY renewal_date""",
            (days,),
        ).fetchall()


# --------------------------------------------------------------------------
# Recurring payments & insights
# --------------------------------------------------------------------------
from app import recurring  # noqa: E402


@app.get("/api/recurring", dependencies=[Depends(authed)])
def get_recurring():
    with db() as conn:
        return recurring.list_recurring(conn)


@app.post("/api/recurring/detect", dependencies=[Depends(authed)])
def run_recurring_detect():
    with db() as conn:
        upserted = recurring.detect(conn)
    return {"upserted": upserted}


@app.get("/api/insights", dependencies=[Depends(authed)])
def get_insights(status: str = "open"):
    with db() as conn:
        return insights.list_insights(conn, status)


class InsightsRunIn(BaseModel):
    llm: bool = False


@app.post("/api/insights/run", dependencies=[Depends(authed)])
def run_insights(body: InsightsRunIn = InsightsRunIn()):
    with db() as conn:
        created = insights.run_rules(conn)
        if body.llm:
            created += insights.run_llm(conn)
    return {"created": created}


class InsightPatch(BaseModel):
    status: str


@app.patch("/api/insights/{insight_id}", dependencies=[Depends(authed)])
def patch_insight(insight_id: str, body: InsightPatch):
    with db() as conn:
        insights.set_status(conn, insight_id, body.status)
    return {"ok": True}


# --------------------------------------------------------------------------
# Payslips
# --------------------------------------------------------------------------
from app import payslip  # noqa: E402


@app.post("/api/payslips/upload", dependencies=[Depends(authed)])
async def upload_payslip(
    member_id: str = Form(...),
    pension_account_id: str | None = Form(None),
    salary_account_id: str | None = Form(None),
    file: UploadFile = File(...),
):
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "file too large")

    with db() as conn:
        db_templates = conn.execute(
            "SELECT name, detect, fields FROM payslip_template"
        ).fetchall()
        templates = list(db_templates) + payslip.DEFAULT_TEMPLATES
        parsed = payslip.parse_pdf(content, templates)

        if not parsed.get("pay_date") or parsed.get("gross_pay") is None \
                or parsed.get("net_pay") is None:
            raise HTTPException(
                422, f"could not parse required payslip fields: {parsed.get('validation')}")

        sha = _hashlib.sha256(content).hexdigest()
        doc = conn.execute(
            """INSERT INTO document (kind, object_key, filename, sha256)
               VALUES ('payslip', %s, %s, %s)
               ON CONFLICT (sha256) DO UPDATE SET filename = EXCLUDED.filename
               RETURNING id""",
            (f"payslip/{sha}", file.filename or "payslip.pdf", sha),
        ).fetchone()

        row = conn.execute(
            """INSERT INTO payslip
                 (document_id, member_id, employer, pay_date, period_start, period_end,
                  tax_code, gross_pay, taxable_pay, income_tax, employee_ni, employer_ni,
                  pension_employee, pension_employer, pension_scheme_type, student_loan,
                  student_loan_plan, other_deductions, net_pay, ytd, parse_method,
                  parse_confidence, status, pension_account_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, 'pending_review', %s)
               ON CONFLICT (member_id, pay_date, gross_pay)
                 DO UPDATE SET document_id = EXCLUDED.document_id
               RETURNING *""",
            (doc["id"], member_id, parsed.get("employer"), parsed["pay_date"],
             parsed.get("period_start"), parsed.get("period_end"), parsed.get("tax_code"),
             parsed["gross_pay"], parsed.get("taxable_pay"), parsed.get("income_tax") or 0,
             parsed.get("employee_ni") or 0, parsed.get("employer_ni"),
             parsed.get("pension_employee") or 0, parsed.get("pension_employer") or 0,
             parsed.get("pension_scheme_type"), parsed.get("student_loan") or 0,
             parsed.get("student_loan_plan"), Json(parsed.get("other_deductions") or []),
             parsed["net_pay"], Json(parsed.get("ytd") or {}), parsed.get("parse_method", "manual"),
             parsed.get("parse_confidence"), pension_account_id),
        ).fetchone()
        conn.commit()

    return {"payslip": row, "validation": parsed.get("validation"),
            "salary_account_id": salary_account_id}


@app.get("/api/payslips", dependencies=[Depends(authed)])
def list_payslips(status: str | None = None):
    with db() as conn:
        if status:
            return conn.execute(
                """SELECT p.*, m.name AS member_name FROM payslip p
                   JOIN member m ON m.id = p.member_id
                   WHERE p.status = %s ORDER BY p.pay_date DESC""",
                (status,),
            ).fetchall()
        return conn.execute(
            """SELECT p.*, m.name AS member_name FROM payslip p
               JOIN member m ON m.id = p.member_id
               ORDER BY p.pay_date DESC"""
        ).fetchall()


@app.get("/api/payslips/{payslip_id}", dependencies=[Depends(authed)])
def get_payslip(payslip_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM payslip WHERE id = %s", (payslip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "payslip not found")
        return row


class PayslipPatch(BaseModel):
    employer: str | None = None
    pay_date: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    tax_code: str | None = None
    gross_pay: float | None = None
    taxable_pay: float | None = None
    income_tax: float | None = None
    employee_ni: float | None = None
    employer_ni: float | None = None
    pension_employee: float | None = None
    pension_employer: float | None = None
    pension_scheme_type: str | None = None
    student_loan: float | None = None
    student_loan_plan: str | None = None
    net_pay: float | None = None
    pension_account_id: str | None = None


@app.patch("/api/payslips/{payslip_id}", dependencies=[Depends(authed)])
def patch_payslip(payslip_id: str, body: PayslipPatch):
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "no fields to update")
    cols = list(updates.keys())
    set_clause = ", ".join(f"{c} = %s" for c in cols)
    params = [updates[c] for c in cols] + [payslip_id]
    with db() as conn:
        row = conn.execute(
            f"UPDATE payslip SET {set_clause} WHERE id = %s RETURNING *", params
        ).fetchone()
        if not row:
            raise HTTPException(404, "payslip not found")
        conn.commit()
        return row


class ConfirmPayslipIn(BaseModel):
    salary_account_id: str


@app.post("/api/payslips/{payslip_id}/confirm", dependencies=[Depends(authed)])
def confirm_payslip(payslip_id: str, body: ConfirmPayslipIn):
    with db() as conn:
        row = conn.execute("SELECT * FROM payslip WHERE id = %s", (payslip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "payslip not found")
        if row["status"] != "pending_review":
            raise HTTPException(409, f"payslip is {row['status']}, not pending review")
        summary = payslip.post_confirmed(conn, row, body.salary_account_id)
        conn.commit()
        return summary


@app.post("/api/payslips/{payslip_id}/reject", dependencies=[Depends(authed)])
def reject_payslip(payslip_id: str):
    with db() as conn:
        row = conn.execute(
            "UPDATE payslip SET status = 'rejected' WHERE id = %s RETURNING *", (payslip_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "payslip not found")
        conn.commit()
        return row


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------
@app.get("/api/trends/networth", dependencies=[Depends(authed)])
def trends_networth(days: int = 365):
    with db() as conn:
        return conn.execute(
            """SELECT snap_date AS date, SUM(value_gbp) AS total_gbp
               FROM snapshot
               WHERE snap_date >= CURRENT_DATE - %s::int
               GROUP BY snap_date ORDER BY snap_date""",
            (days,),
        ).fetchall()


@app.get("/api/trends/spend", dependencies=[Depends(authed)])
def trends_spend(months: int = 6):
    with db() as conn:
        return conn.execute(
            """SELECT to_char(t.posted_at, 'YYYY-MM') AS year_month,
                      COALESCE(c.name, 'Uncategorised') AS category,
                      SUM(-t.amount) AS amount
               FROM transaction t LEFT JOIN category c ON c.id = t.category_id
               WHERE t.amount < 0
                 AND t.posted_at >= date_trunc('month', CURRENT_DATE)
                                    - (%s::text || ' months')::interval
                 AND (c.kind IS NULL OR c.kind NOT IN ('transfer', 'tax', 'pension'))
               GROUP BY year_month, category
               ORDER BY year_month, category""",
            (months,),
        ).fetchall()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
import io  # noqa: E402

from fastapi.responses import StreamingResponse  # noqa: E402

from app import export, taxyear  # noqa: E402


@app.get("/api/export/{tax_year}", dependencies=[Depends(authed)])
def export_data(tax_year: str, format: str = "json"):
    if format not in ("json", "csv"):
        raise HTTPException(422, "format must be 'json' or 'csv'")
    try:
        ty = taxyear.parse_tax_year(tax_year)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db() as conn:
        content, media_type, filename = export.export_tax_year(conn, ty, format)
    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Investments: file import, holdings, provider sync
# --------------------------------------------------------------------------
from app import coinbase, investimport, trading212  # noqa: E402


@app.post("/api/import/investments", dependencies=[Depends(authed)])
async def import_investments(
    account_id: str = Form(...),
    file: UploadFile = File(...),
):
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "file too large")

    with db() as conn:
        acc = conn.execute(
            "SELECT id, currency FROM account WHERE id = %s", (account_id,)
        ).fetchone()
        if not acc:
            raise HTTPException(404, "account not found")

        parsed = investimport.parse(content, file.filename)

        if parsed["kind"] == "holdings" and parsed["rows"]:
            n = investimport.apply_holdings(conn, account_id, parsed["rows"])
            conn.commit()
            return {"kind": "holdings", "applied": n, "warnings": parsed["warnings"]}

        sha = _hashlib.sha256(content).hexdigest()
        doc = conn.execute(
            """INSERT INTO document (kind, object_key, filename, sha256)
               VALUES ('csv', %s, %s, %s)
               ON CONFLICT (sha256) DO UPDATE SET filename = EXCLUDED.filename
               RETURNING id""",
            (f"csv/{sha}", file.filename or "import.csv", sha),
        ).fetchone()

        if parsed["kind"] == "transactions" and parsed["rows"]:
            inserted, ids = _insert_txns(conn, acc["id"], acc["currency"], parsed["rows"], doc["id"])
            conn.commit()
            categorised = categorise.apply_rules(conn, ids) if ids else 0
            return {"kind": "transactions", "imported": inserted, "categorised": categorised,
                    "warnings": parsed["warnings"]}

        # generic fallback — try the plain bank-CSV parser
        try:
            csv_parsed = csvimport.parse(content)
        except ValueError as e:
            raise HTTPException(
                422, f"unrecognised investment file and csv fallback failed: {e}")
        inserted, ids = _insert_txns(conn, acc["id"], acc["currency"], csv_parsed["rows"], doc["id"])
        conn.commit()
        categorised = categorise.apply_rules(conn, ids) if ids else 0
        return {"kind": "generic_csv", "imported": inserted, "categorised": categorised,
                "warnings": csv_parsed["warnings"] + parsed["warnings"]}


@app.post("/api/sync/trading212", dependencies=[Depends(authed)])
def sync_trading212():
    if not trading212.configured():
        raise HTTPException(400, "TRADING212_API_KEY not configured")
    trading212.sync_all()
    return {"ok": True}


@app.post("/api/sync/coinbase", dependencies=[Depends(authed)])
def sync_coinbase():
    if not coinbase.configured():
        raise HTTPException(400, "COINBASE_API_KEY/COINBASE_API_SECRET not configured")
    coinbase.sync_all()
    return {"ok": True}


@app.get("/api/holdings", dependencies=[Depends(authed)])
def list_holdings(account_id: str | None = None):
    where = "WHERE h.account_id = %s" if account_id else ""
    params = (account_id,) if account_id else ()
    with db() as conn:
        return conn.execute(
            f"""SELECT h.account_id, a.name AS account_name, i.symbol,
                       i.name AS instrument_name, i.kind, h.quantity, h.avg_cost, h.as_of
                FROM holding h
                JOIN instrument i ON i.id = h.instrument_id
                JOIN account a ON a.id = h.account_id
                {where}
                ORDER BY a.name, i.symbol""",
            params,
        ).fetchall()
