"""Migration runner: applies numbered SQL files from /app/migrations exactly once.

001 is the base schema (db/schema.sql copied in by the Dockerfile). Add new
migrations as 002_*.sql, 003_*.sql — never edit an applied file.
"""
import hashlib
import os
import pathlib
import time

import psycopg

MIGRATIONS_DIR = pathlib.Path(os.getenv("MIGRATIONS_DIR", "/app/migrations"))


def connect(retries: int = 30) -> psycopg.Connection:
    dsn = os.environ["DATABASE_URL"]
    last: Exception | None = None
    for _ in range(retries):
        try:
            return psycopg.connect(dsn)
        except psycopg.OperationalError as e:  # postgres still booting
            last = e
            time.sleep(2)
    raise SystemExit(f"could not reach postgres: {last}")


def run() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"no migrations found in {MIGRATIONS_DIR}")

    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migration (
                   filename TEXT PRIMARY KEY,
                   sha256   TEXT NOT NULL,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        applied = {
            r[0]: r[1]
            for r in conn.execute("SELECT filename, sha256 FROM schema_migration")
        }
        for f in files:
            sql = f.read_text()
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if f.name in applied:
                if applied[f.name] != digest:
                    raise SystemExit(
                        f"{f.name} changed after being applied — create a new migration instead"
                    )
                continue
            print(f"applying {f.name}")
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migration (filename, sha256) VALUES (%s, %s)",
                (f.name, digest),
            )
        conn.commit()
    print("migrations up to date")
