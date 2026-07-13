"""Rotas HTTP da API de análise."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.config import settings
from app.integrations.mercadolivre import normalize_mlb_id
from app.models.db import Analysis, SessionLocal
from app.schemas.analysis import (
    ActionItemOut,
    AnalysisResultResponse,
    CreateAnalysisRequest,
    CreateAnalysisResponse,
    ScoreOut,
)

router = APIRouter(prefix="/api", tags=["analysis"])


@router.post("/analysis", response_model=CreateAnalysisResponse)
def create_analysis(
    body: CreateAnalysisRequest, background: BackgroundTasks
) -> CreateAnalysisResponse:
    mlb_id = normalize_mlb_id(body.input)
    if not mlb_id:
        raise HTTPException(
            status_code=422,
            detail="Informe um código MLB válido ou o link do anúncio.",
        )

    session = SessionLocal()
    try:
        analysis = Analysis(mlb_id=mlb_id, status="pending")
        session.add(analysis)
        session.commit()
        analysis_id = analysis.id
    finally:
        session.close()

    if settings.use_celery:
        from app.workers.queue import enqueue_analysis

        enqueue_analysis(analysis_id)
    else:
        # modo simples: roda na própria API, sem worker/Redis
        from app.workers.orchestrator import run_analysis

        background.add_task(run_analysis, analysis_id)

    return CreateAnalysisResponse(analysis_id=analysis_id, status="pending")


@router.get("/analysis/{analysis_id}", response_model=AnalysisResultResponse)
def get_analysis(analysis_id: str) -> AnalysisResultResponse:
    session = SessionLocal()
    try:
        analysis = session.get(Analysis, analysis_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="Análise não encontrada.")

        return AnalysisResultResponse(
            analysis_id=analysis.id,
            mlb_id=analysis.mlb_id,
            status=analysis.status,
            general_score=analysis.general_score,
            classification=analysis.classification,
            consultant_summary=analysis.consultant_summary,
            error=analysis.error,
            scores=[
                ScoreOut(module=s.module, score=s.score, details=s.details or {})
                for s in analysis.scores
            ],
            action_items=[
                ActionItemOut(
                    priority=a.priority,
                    order=a.order,
                    title=a.title,
                    impact=a.impact,
                    difficulty=a.difficulty,
                    estimated_time=a.estimated_time,
                )
                for a in sorted(analysis.action_items, key=lambda x: x.order)
            ],
        )
    finally:
        session.close()
