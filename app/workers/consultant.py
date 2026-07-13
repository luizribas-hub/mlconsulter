"""
Camada "Consultor IA" (Módulos 12 e 14).

Recebe TODOS os resultados estruturados dos módulos (não texto solto) e produz:
  - um resumo em linguagem de consultor ("o que eu faria se fosse meu")
  - um plano de ação priorizado (alta/média/baixa) com impacto, dificuldade e tempo

Regra de ouro: a IA NÃO calcula scores. Ela recebe os números já calculados
e apenas explica, prioriza e recomenda. Isso mantém o produto consistente,
auditável e barato.

No MVP há um fallback determinístico (build_from_findings) que gera o plano
a partir dos findings dos módulos, sem depender da API de IA — útil para
desenvolver e testar o pipeline antes de plugar o modelo. Quando a
ANTHROPIC_API_KEY estiver configurada, a síntese textual usa o modelo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.modules.base import AnalysisContext

SEVERITY_TO_PRIORITY = {
    "critico": "alta",
    "atencao": "media",
    "positivo": "baixa",
}


@dataclass
class ConsultantOutput:
    summary: str
    action_items: list[dict[str, Any]] = field(default_factory=list)


def _collect_findings(ctx: AnalysisContext) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for result in ctx.results.values():
        for f in result.findings:
            items.append(
                {
                    "module": result.module,
                    "severity": f.severity,
                    "message": f.message,
                    "impact": f.impact_hint,
                }
            )
    return items


def build_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback determinístico: transforma findings em plano de ação."""
    order = {"critico": 0, "atencao": 1, "positivo": 2}
    ordered = sorted(findings, key=lambda f: order.get(f["severity"], 3))
    plan: list[dict[str, Any]] = []
    for f in ordered:
        if f["severity"] == "positivo":
            continue  # positivos não viram ação
        plan.append(
            {
                "priority": SEVERITY_TO_PRIORITY.get(f["severity"], "media"),
                "title": f["message"],
                "impact": f.get("impact"),
                "difficulty": None,
                "estimated_time": None,
            }
        )
    return plan


def _summary_with_ai(ctx: AnalysisContext, general: float | None,
                     findings: list[dict[str, Any]]) -> str:
    """
    Gera a narrativa de consultor com o modelo da Anthropic.
    Só é chamada quando há API key. Import local para não exigir a lib
    quando o fallback é usado.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    payload = {
        "titulo": ctx.item.title,
        "preco": ctx.item.price,
        "nota_geral": general,
        "concorrentes": ctx.shared.get("benchmark_aggregates"),
        "achados": findings,
    }
    prompt = (
        "Você é um consultor especialista em otimização de anúncios do "
        "Mercado Livre. Com base SOMENTE nos dados estruturados abaixo "
        "(já calculados), escreva uma análise objetiva respondendo: "
        "'o que eu faria se este anúncio fosse meu?'. Seja direto, use "
        "linguagem de consultor, foque em aumento de vendas. Não invente "
        "métricas que não estão nos dados.\n\n"
        f"DADOS:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    msg = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    )


def build_consultant_output(ctx: AnalysisContext,
                            general: float | None) -> ConsultantOutput:
    findings = _collect_findings(ctx)
    action_items = build_from_findings(findings)

    if settings.anthropic_api_key:
        try:
            summary = _summary_with_ai(ctx, general, findings)
        except Exception:  # noqa: BLE001 - IA nunca deve derrubar a análise
            summary = _summary_fallback(ctx, general, findings)
    else:
        summary = _summary_fallback(ctx, general, findings)

    return ConsultantOutput(summary=summary, action_items=action_items)


def _summary_fallback(ctx: AnalysisContext, general: float | None,
                      findings: list[dict[str, Any]]) -> str:
    n = len([f for f in findings if f["severity"] != "positivo"])
    nota = f"{general}/100" if general is not None else "ainda não pontuável"
    return (
        f"Análise do anúncio '{ctx.item.title}'. Nota geral: {nota}. "
        f"Foram identificados {n} pontos de melhoria priorizados no plano "
        f"de ação. (Resumo gerado sem IA — configure ANTHROPIC_API_KEY para "
        f"a narrativa completa do consultor.)"
    )
