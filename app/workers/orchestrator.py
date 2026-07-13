"""
Orquestrador do pipeline de análise.

Executa os módulos na ordem de dependência, monta o contexto compartilhado,
calcula a nota geral (ponderada e determinística) e persiste tudo.

No MVP roda em processo único (chamado pelo worker). Como cada módulo é
independente e comunica via ctx.shared, extrair para serviços/paralelizar
depois não exige reescrever a lógica.

A camada "consultor IA" (síntese + plano de ação) é chamada ao final —
está isolada em app.workers.consultant para poder evoluir sem tocar aqui.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from app.integrations.mercadolivre import (
    MercadoLivreClient,
    MercadoLivreError,
    normalize_mlb_id,
)
from app.integrations.normalizer import snapshot_from_item
from app.models.db import ActionItem, Analysis, AnalysisScore, SessionLocal
from app.modules.base import AnalysisContext, ModuleResult
from app.modules.benchmark import BenchmarkModule
from app.modules.price import PriceModule
from app.modules.reputation import ReputationModule

# Ordem de execução do MVP. Benchmark primeiro (é insumo dos demais).
# (Título, Posicionamento e Fotos entram nos próximos passos.)
PIPELINE = [
    BenchmarkModule(),
    PriceModule(),
    ReputationModule(),
]

# Pesos para a nota geral. Só entram módulos que produzem score (0..100).
# Benchmark não tem score próprio (é insumo), então não pesa aqui.
# Pesos serão rebalanceados quando título/fotos/posição entrarem.
SCORE_WEIGHTS: dict[str, float] = {
    "price": 0.55,
    "reputation": 0.45,
    # "title": ...,
    # "photos": ...,
    # "search_position": ...,
}


def _classify(score: float) -> str:
    if score >= 90:
        return "Excelente"
    if score >= 75:
        return "Muito Bom"
    if score >= 60:
        return "Bom"
    if score >= 40:
        return "Regular"
    return "Crítico"


def _weighted_general_score(results: dict[str, ModuleResult]) -> Optional[float]:
    num, den = 0.0, 0.0
    for module, weight in SCORE_WEIGHTS.items():
        r = results.get(module)
        if r and r.score is not None:
            num += r.score * weight
            den += weight
    if den == 0:
        return None
    return round(num / den, 1)


def run_analysis(analysis_id: str) -> None:
    """Ponto de entrada chamado pelo worker (fila)."""
    session = SessionLocal()
    analysis = session.get(Analysis, analysis_id)
    if analysis is None:
        session.close()
        return

    analysis.status = "running"
    session.commit()

    mlb_id = normalize_mlb_id(analysis.mlb_id)
    if not mlb_id:
        analysis.status = "failed"
        analysis.error = "MLB inválido ou não reconhecido."
        analysis.finished_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
        session.close()
        return

    try:
        with MercadoLivreClient() as client:
            item_raw = client.get_item(mlb_id)
            if not item_raw:
                raise MercadoLivreError(f"Anúncio {mlb_id} não encontrado.")

            snapshot = snapshot_from_item(item_raw)
            ctx = AnalysisContext(item=snapshot, client=client)

            for module in PIPELINE:
                result = module.run(ctx)
                ctx.results[module.name] = result
                session.add(
                    AnalysisScore(
                        analysis_id=analysis.id,
                        module=result.module,
                        score=result.score,
                        details=result.as_dict()["data"],
                    )
                )

            general = _weighted_general_score(ctx.results)

            # ---- camada consultor (IA) -------------------------------
            from app.workers.consultant import build_consultant_output

            consultant = build_consultant_output(ctx, general)
            analysis.consultant_summary = consultant.summary
            for idx, ai in enumerate(consultant.action_items):
                session.add(
                    ActionItem(
                        analysis_id=analysis.id,
                        priority=ai["priority"],
                        order=idx,
                        title=ai["title"],
                        impact=ai.get("impact"),
                        difficulty=ai.get("difficulty"),
                        estimated_time=ai.get("estimated_time"),
                    )
                )

            analysis.general_score = general
            analysis.classification = _classify(general) if general is not None else None
            analysis.status = "done"
            analysis.finished_at = dt.datetime.now(dt.timezone.utc)
            session.commit()

    except Exception as exc:  # noqa: BLE001 - queremos registrar qualquer falha
        session.rollback()
        analysis.status = "failed"
        analysis.error = str(exc)[:500]
        analysis.finished_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
    finally:
        session.close()
