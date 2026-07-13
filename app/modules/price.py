"""Módulo 7 — Preço. Compara o preço do anúncio com os agregados do nicho
(vindos do Benchmark via ctx.shared). Score determinístico."""
from __future__ import annotations

from app.modules.base import AnalysisContext, AnalysisModule, Finding, ModuleResult


class PriceModule(AnalysisModule):
    name = "price"

    def run(self, ctx: AnalysisContext) -> ModuleResult:
        item = ctx.item
        agg = ctx.shared.get("benchmark_aggregates") or {}
        result = ModuleResult(module=self.name, score=None)

        price = item.price
        median = agg.get("preco_mediano")

        if price is None or not median:
            result.findings.append(
                Finding("atencao", "Sem base de preço do nicho para comparar.")
            )
            return result

        diff_pct = round((price - median) / median * 100, 1)
        result.data = {
            "preco": price,
            "mediana_nicho": median,
            "diff_percentual": diff_pct,
        }

        # Score: 100 quando alinhado à mediana; cai conforme se afasta acima.
        # Ficar abaixo penaliza menos (barato converte, mas corrói margem).
        if diff_pct <= 0:
            score = max(70.0, 100 + diff_pct)  # até -30% mantém bom score
            if diff_pct < -15:
                result.findings.append(
                    Finding(
                        "atencao",
                        f"Preço {abs(diff_pct)}% abaixo da mediana — verifique margem.",
                        "Pode estar deixando dinheiro na mesa.",
                    )
                )
        else:
            score = max(0.0, 100 - diff_pct * 2)  # acima penaliza o dobro
            sev = "critico" if diff_pct > 15 else "atencao"
            result.findings.append(
                Finding(
                    sev,
                    f"Preço {diff_pct}% acima da mediana do nicho.",
                    "Preço alto reduz conversão frente aos concorrentes.",
                )
            )

        result.score = round(score, 1)
        return result
