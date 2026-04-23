"""FastAPI-App – Einstiegspunkt."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, engine
from .routers import admin_pages, helper_area, public


def create_app() -> FastAPI:
    # DB-Tabellen erstellen, falls nicht vorhanden
    Base.metadata.create_all(bind=engine)

    app = FastAPI(title=f"{settings.FESTIVAL_NAME} – Helfer-Tool", docs_url=None, redoc_url=None)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    app.include_router(public.router)
    app.include_router(helper_area.router)
    app.include_router(admin_pages.router)

    return app


app = create_app()
