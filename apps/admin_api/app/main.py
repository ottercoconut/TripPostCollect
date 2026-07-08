"""TripPostCollect read-only admin API."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.admin_api.app.routers import health, maintenance, records


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
    return app


app = create_app()
