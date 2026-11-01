"""FastAPI entrypoint for the job copilot web UI.

Binds to 127.0.0.1 (see start.py). Serves the JSON API under /api and, when the React app
has been built, the static bundle from web/frontend/dist at /. Remote access arrives through
a Cloudflare Tunnel, and auth.gate guards every path.

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

from . import auth
from .routes import router

app = FastAPI(title="Job Pipeline Console", version="1.0")

app.middleware("http")(auth.gate)

# Local dev: the Vite dev server (5173) calls the API on 8000. Loopback only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.add_api_route("/login", auth.login_form, methods=["GET"], include_in_schema=False)
app.add_api_route("/login", auth.login_submit, methods=["POST"], include_in_schema=False)
app.add_api_route("/logout", auth.logout, methods=["POST"], include_in_schema=False)

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
        # Resolve before the check: an encoded `..` in the URL would otherwise read files
        # outside dist/.
        candidate = (DIST / full_path).resolve()
        if candidate.is_relative_to(DIST.resolve()) and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(DIST / "index.html"))
