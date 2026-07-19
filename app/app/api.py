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
_sessions: dict[str, datetime] = {}  # token -> expiry (single-user; in-memory is fine)


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
    _sessions[token] = datetime.now(timezone.utc) + SESSION_TTL
    return {"token": token}


def authed(request: Request) -> None:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    expiry = _sessions.get(token)
    if not expiry or expiry < datetime.now(timezone.utc):
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


@app.get("/api/accounts", dependencies=[Depends(authed)])
def list_accounts():
    with db() as conn:
        return conn.execute(
            """SELECT a.*, json_agg(json_build_object('member_id', o.member_id,
                                                      'share_pct', o.share_pct)) AS owners
               FROM account a LEFT JOIN account_owner o ON o.account_id = a.id
               WHERE NOT a.archived GROUP BY a.id ORDER BY a.created_at"""
        ).fetchall()


@app.post("/api/accounts", dependencies=[Depends(authed)])
def create_account(a: AccountIn):
    with db() as conn:
        hh = conn.execute("SELECT id FROM household LIMIT 1").fetchone()
        row = conn.execute(
            """INSERT INTO account (household_id, name, type, currency, is_liability)
               VALUES (%s, %s, %s::account_type, %s, %s) RETURNING *""",
            (hh["id"], a.name, a.type, a.currency, a.is_liability),
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
def list_transactions(limit: int = 50):
    with db() as conn:
        return conn.execute(
            """SELECT t.id, t.posted_at, t.amount, t.currency, t.description,
                      a.name AS account_name
               FROM transaction t JOIN account a ON a.id = t.account_id
               ORDER BY t.posted_at DESC, t.created_at DESC LIMIT %s""",
            (min(limit, 200),),
        ).fetchall()
