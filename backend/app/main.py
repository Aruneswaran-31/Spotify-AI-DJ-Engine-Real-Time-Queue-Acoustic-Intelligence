"""AI DJ Studio application entrypoint."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .catalog import catalog
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
    logger.info("AI DJ Studio ready: %d rooms, %d tracks", len(hub.rooms), len(catalog.all()))
    yield
    await hub.stop()


app = FastAPI(
    title="AI DJ Studio API",
    version="1.0.0",
    description="Collaborative live queue curation & sentiment-aware audio engine.",
    lifespan=lifespan,
)

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


@app.get("/api/v1/health")
def health() -> dict:
    return {
        "status": "ok",
        "uptimeSeconds": round(time.time() - STARTED_AT, 1),
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
