"""Backup: pg_dump -Fc | age encrypt -> Backblaze B2 (S3 API), then prune.

Requires in the image: pg_dump (postgresql-client) and age.
Env: DATABASE_URL, AGE_PUBLIC_KEY, B2_BUCKET, B2_KEY_ID, B2_APP_KEY,
     B2_ENDPOINT (default eu-central B2 S3 endpoint), RETENTION_JSON.
"""
import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone

import boto3


def _client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("B2_ENDPOINT", "https://s3.eu-central-003.backblazeb2.com"),
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APP_KEY"],
    )


def run() -> None:
    bucket = os.environ["B2_BUCKET"]
    age_key = os.environ["AGE_PUBLIC_KEY"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"pg/budget-{stamp}.dump.age"

    with tempfile.NamedTemporaryFile(suffix=".dump.age") as tmp:
        dump = subprocess.Popen(
            ["pg_dump", "--format=custom", os.environ["DATABASE_URL"]],
            stdout=subprocess.PIPE,
        )
        enc = subprocess.run(
            ["age", "-r", age_key, "-o", tmp.name], stdin=dump.stdout, check=False
        )
        if dump.wait() != 0 or enc.returncode != 0:
            raise SystemExit("pg_dump/age failed")
        _client().upload_file(tmp.name, bucket, key)
        print(f"uploaded {key} ({os.path.getsize(tmp.name)} bytes)")

    prune(bucket)


def prune(bucket: str) -> None:
    """Keep N daily, N weekly (Mondays), N monthly (1sts); delete the rest."""
    ret = json.loads(os.getenv("RETENTION_JSON", '{"daily":7,"weekly":4,"monthly":12}'))
    s3 = _client()
    objs = s3.list_objects_v2(Bucket=bucket, Prefix="pg/").get("Contents", [])
    today = date.today()
    keep: set[str] = set()

    def dated(o):
        try:
            return date.fromisoformat(o["Key"].split("budget-")[1][:10])
        except Exception:
            return None

    for o in objs:
        d = dated(o)
        if d is None:
            keep.add(o["Key"])  # never delete what we can't parse
            continue
        if d >= today - timedelta(days=ret["daily"]):
            keep.add(o["Key"])
        elif d.weekday() == 0 and d >= today - timedelta(weeks=ret["weekly"]):
            keep.add(o["Key"])
        elif d.day == 1 and d >= today - timedelta(days=31 * ret["monthly"]):
            keep.add(o["Key"])

    for o in objs:
        if o["Key"] not in keep:
            s3.delete_object(Bucket=bucket, Key=o["Key"])
            print(f"pruned {o['Key']}")
