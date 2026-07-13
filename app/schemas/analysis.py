"""Schemas de entrada/saída da API (contratos entre frontend e backend)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class CreateAnalysisRequest(BaseModel):
    # aceita MLB puro OU link completo do anúncio
    input: str


class CreateAnalysisResponse(BaseModel):
    analysis_id: str
    status: str


class ActionItemOut(BaseModel):
    priority: str
    order: int
    title: str
    impact: Optional[str] = None
    difficulty: Optional[str] = None
    estimated_time: Optional[str] = None


class ScoreOut(BaseModel):
    module: str
    score: Optional[float] = None
    details: dict[str, Any] = {}


class AnalysisResultResponse(BaseModel):
    analysis_id: str
    mlb_id: str
    status: str
    general_score: Optional[float] = None
    classification: Optional[str] = None
    consultant_summary: Optional[str] = None
    scores: list[ScoreOut] = []
    action_items: list[ActionItemOut] = []
    error: Optional[str] = None
