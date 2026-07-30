"""FastAPI entrypoint for the job copilot web UI.

Local-first: binds to 127.0.0.1 (see start.py). Serves the JSON API under /api and,
when the React app has been built, the static bundle from web/frontend/dist at /.

The web app is a SEPARATE process from the bridge on purpose — it shares the same
on-disk state and reuses the pipeline's modules, but never opens a WebSocket to the
bridge (which supersedes on connect and would kill Parth's live extension review).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import router

app = FastAPI(title="Job Pipeline Console", version="1.0")

# Local dev: the Vite dev server (5173) calls the API on 8000. Loopback only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


@app.get("/api/health")
def health():
    return {"ok": True}


if DIST.exists():
    # Serve the built SPA. assets/ is hashed; everything else falls back to index.html so
    # client-side routes deep-link cleanly.
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(DIST / "index.html"))

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(DIST / "index.html"))
