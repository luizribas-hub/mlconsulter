"""Módulo 9 — Reputação do vendedor. Lê /users/{seller_id} e pontua com base
no nível e nas métricas públicas de reputação. Score determinístico."""
from __future__ import annotations

from app.modules.base import AnalysisContext, AnalysisModule, Finding, ModuleResult

# power_seller_status -> peso base
LEVEL_SCORE = {"platinum": 100, "gold": 85, "silver": 70}


class ReputationModule(AnalysisModule):
    name = "reputation"

    def run(self, ctx: AnalysisContext) -> ModuleResult:
        result = ModuleResult(module=self.name, score=None)
        seller_id = ctx.item.seller_id
        if not seller_id:
            result.findings.append(Finding("atencao", "Vendedor não identificado."))
            return result

        user = ctx.client.get_user(seller_id) or {}
        rep = user.get("seller_reputation") or {}
        level = rep.get("power_seller_status")
        color = rep.get("level_id")  # ex.: "5_green"
        metrics = rep.get("metrics") or {}

        base = LEVEL_SCORE.get(level or "", 55.0)

        # penaliza reclamações/cancelamentos altos (rate 0..1)
        def rate(key: str) -> float:
            return (metrics.get(key) or {}).get("rate", 0.0) or 0.0

        claims = rate("claims")
        cancellations = rate("cancellations")
        penalty = (claims + cancellations) * 100  # cada 1% tira ~1 ponto
        score = max(0.0, base - penalty)

        result.data = {
            "power_seller_status": level,
            "level_id": color,
            "claims_rate": claims,
            "cancellations_rate": cancellations,
        }

        if color and color.endswith(("red", "orange")):
            result.findings.append(
                Finding(
                    "critico",
                    "Reputação do vendedor em faixa de risco (vermelho/laranja).",
                    "Afeta diretamente a confiança e a conversão.",
                )
            )
        elif not level:
            result.findings.append(
                Finding(
                    "atencao",
                    "Vendedor ainda sem selo (Prata/Ouro/Platina).",
                    "Selo aumenta a confiança do comprador.",
                )
            )

        result.score = round(score, 1)
        return result
