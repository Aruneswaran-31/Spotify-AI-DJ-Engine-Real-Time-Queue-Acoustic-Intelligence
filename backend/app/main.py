"""AI DJ Studio application entrypoint."""

from __future__ import annotations

import base64
import logging
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .catalog import catalog
from .cluster import cluster
from .config import settings
from .db import SessionLocal, init_db
from .realtime import hub
from .routers import ai, analytics, auth, lounges, tracks
from .ws import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("aidj.main")
STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        catalog.refresh(db)
    await hub.start()
    if settings.JWT_SECRET_IS_EPHEMERAL:
        logger.warning("JWT_SECRET is not set — using an ephemeral key; tokens will not survive a restart")
    logger.info(
        "AI DJ Studio ready: %d rooms, %d tracks, worker %s (bus=%s)",
        len(hub.rooms),
        len(catalog.all()),
        cluster.process_id,
        cluster.backend,
    )
    yield
    await hub.stop()


app = FastAPI(
    title="AI DJ Studio API",
    version="1.0.0",
    description="Collaborative live queue curation & sentiment-aware audio engine.",
    lifespan=lifespan,
)

LOUNGE_PATH = re.compile(r"^/api/v1/lounges/([^/]+)")
_SKIP_HEADERS = {
    "host",
    "content-length",
    "content-encoding",
    "connection",
    "transfer-encoding",
    "accept-encoding",
}


async def _execute_forwarded(payload: dict) -> dict:
    """Run a lounge-scoped request locally, on the worker that owns the room."""
    headers = {
        key: value
        for key, value in (payload.get("headers") or {}).items()
        if key.lower() not in _SKIP_HEADERS
    }
    headers["x-aidj-internal"] = "1"
    body = base64.b64decode(payload["body"]) if payload.get("body") else None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://internal") as client:
        response = await client.request(
            payload.get("method", "GET"),
            payload.get("path", "/"),
            params=payload.get("query") or None,
            headers=headers,
            content=body,
        )
    return {
        "status": response.status_code,
        "headers": {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-length", "transfer-encoding", "content-encoding")
        },
        "body": base64.b64encode(response.content).decode(),
    }


class ClusterProxyMiddleware(BaseHTTPMiddleware):
    """Route lounge-scoped requests to whichever worker owns that room's live state."""

    async def dispatch(self, request, call_next):
        if not cluster.enabled or request.headers.get("x-aidj-internal"):
            return await call_next(request)
        match = LOUNGE_PATH.match(request.url.path)
        if match is None:
            return await call_next(request)
        lounge_id = match.group(1)
        if await cluster.claim(lounge_id):
            return await call_next(request)
        try:
            result = await cluster.rpc.request(
                cluster.room_channel(lounge_id),
                "http.request",
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "headers": dict(request.headers),
                    "body": base64.b64encode(await request.body()).decode(),
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to forward %s %s", request.method, request.url.path)
            return JSONResponse(
                {"detail": "That room is being served by an unavailable worker, please retry"},
                status_code=503,
            )
        return Response(
            content=base64.b64decode(result.get("body") or ""),
            status_code=int(result.get("status", 502)),
            headers=result.get("headers") or {},
        )


# Registered before CORS so that CORS stays the outermost layer and still decorates
# responses that this middleware forwards.
app.add_middleware(ClusterProxyMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for api_router in (auth.router, tracks.router, tracks.admin_router, lounges.router, ai.router, analytics.router):
    app.include_router(api_router, prefix=settings.API_PREFIX)
app.include_router(ws_router)


hub.http_executor = _execute_forwarded


@app.get("/api/v1/health")
def health() -> dict:
    return {
        "status": "ok",
        "uptimeSeconds": round(time.time() - STARTED_AT, 1),
        "worker": cluster.process_id,
        "bus": cluster.backend,
        "rooms": len(hub.rooms),
        "tracks": len(catalog.all()),
        "llm": "enabled" if settings.LLM_ENABLED else "deterministic fallback",
    }


@app.get("/api/v1/health/ws")
def ws_health() -> dict:
    return {
        "status": "ok",
        "rooms": len(hub.rooms),
        "connections": sum(len(room.connections) for room in hub.rooms.values()),
    }


settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/audio", StaticFiles(directory=str(settings.AUDIO_DIR)), name="audio")

if settings.FRONTEND_DIST.exists():
    assets = settings.FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith(("api/", "ws/", "static/", "assets/")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = settings.FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = settings.FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "Frontend build missing"}, status_code=404)
else:

    @app.get("/")
    def root() -> dict:
        return {
            "service": settings.APP_NAME,
            "docs": "/docs",
            "api": settings.API_PREFIX,
            "note": "Build the React client (frontend) to serve the UI from this process.",
        }
