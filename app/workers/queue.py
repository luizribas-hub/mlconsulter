"""
Fila de processamento assíncrono (Celery + Redis).

O BFF apenas enfileira `process_analysis`; os workers rodam o pipeline.
Assim a requisição HTTP retorna na hora e o processamento (que pode levar
alguns segundos por causa das chamadas ao ML e à IA) roda fora do request.

Rodar o worker:
    celery -A app.workers.queue.celery_app worker --loglevel=info
"""
from __future__ import annotations

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "mlaudit",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
)


@celery_app.task(name="process_analysis")
def process_analysis(analysis_id: str) -> None:
    # import local para evitar carregar o pipeline no processo do BFF
    from app.workers.orchestrator import run_analysis

    run_analysis(analysis_id)


def enqueue_analysis(analysis_id: str) -> None:
    process_analysis.delay(analysis_id)
