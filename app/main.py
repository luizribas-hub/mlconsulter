"""Entrypoint da API (BFF).

Rodar:
    uvicorn app.main:app --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api.routes import router
from app.core.config import settings
from app.models.db import init_db

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrinja em produção
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

_INDEX = Path(__file__).parent / "frontend" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX.read_text(encoding="utf-8")


@app.on_event("startup")
def _startup() -> None:
    # No MVP criamos as tabelas na subida. Em produção, use Alembic.
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
