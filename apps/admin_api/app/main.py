"""TripPostCollect read-only admin API."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.admin_api.app.routers import captures, health, images, maintenance, overview, platforms, records, scheduler
from apps.admin_api.app.settings import load_settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="TripPostCollect Admin API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(maintenance.router)
    app.include_router(records.router)
    app.include_router(images.router)
    app.include_router(captures.router)
    app.include_router(overview.router)
    app.include_router(platforms.router)
    app.include_router(scheduler.router)
    _mount_admin_web(app, load_settings().static_dir)
    return app


def _mount_admin_web(app: FastAPI, static_dir: Path) -> None:
    """Serve the production SPA from the API process when a build is present."""
    index_path = static_dir / "index.html"
    if not index_path.is_file():
        return

    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="admin-assets")

    @app.get("/", include_in_schema=False)
    def admin_index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/{request_path:path}", include_in_schema=False)
    def admin_spa(request_path: str) -> FileResponse:
        if request_path == "api" or request_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")

        static_root = static_dir.resolve()
        candidate = (static_root / request_path).resolve()
        if static_root == candidate or static_root in candidate.parents:
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index_path)


app = create_app()
