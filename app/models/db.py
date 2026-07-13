"""
Modelos de banco de dados (SQLAlchemy 2.x).

Núcleo do produto:
  analyses        -> uma auditoria de um MLB
  analysis_scores -> nota + payload por módulo
  action_items    -> plano de ação priorizado (o entregável do consultor)

O JSONB permite mudar a forma do payload de um módulo sem migração pesada,
e `scoring_version` deixa reprocessar análises antigas de forma segura.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.core.config import settings

connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine = create_engine(settings.database_url, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mlb_id: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | running | done | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    general_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    consultant_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    scoring_version: Mapped[str] = mapped_column(String(20), default="v1")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    scores: Mapped[list["AnalysisScore"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )
    action_items: Mapped[list["ActionItem"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


class AnalysisScore(Base):
    __tablename__ = "analysis_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"))
    module: Mapped[str] = mapped_column(String(40), index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    analysis: Mapped["Analysis"] = relationship(back_populates="scores")


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"))
    priority: Mapped[str] = mapped_column(String(10))  # alta | media | baixa
    order: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(255))
    impact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    estimated_time: Mapped[str | None] = mapped_column(String(40), nullable=True)

    analysis: Mapped["Analysis"] = relationship(back_populates="action_items")


def init_db() -> None:
    """Cria as tabelas. Em produção, prefira Alembic para migrações."""
    Base.metadata.create_all(engine)
