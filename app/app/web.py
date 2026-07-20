"""Web: serves the SPA and proxies /api/* to the api service in-cluster."""
import os
import pathlib

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

STATIC = pathlib.Path(__file__).parent / "web-static"
API_URL = os.getenv("API_URL", "http://family-budget-api:8000")

app = FastAPI()


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request) -> Response:
    async with httpx.AsyncClient(base_url=API_URL, timeout=60) as client:
        upstream = await client.request(
            request.method,
            f"/api/{path}",
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=await request.body(),
            params=request.query_params,
        )
    return Response(upstream.content, upstream.status_code,
                    media_type=upstream.headers.get("content-type"))


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/{path:path}")
def spa(path: str):
    target = STATIC / path
    if path and target.is_file():
        return FileResponse(target)
    # Never let browsers/proxies cache the SPA shell — it embeds the JS that
    # holds auth restore logic; a stale index.html looks like "I'm signed out".
    return FileResponse(
        STATIC / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )
