"""
Módulo 4 — Benchmark automático.

Localiza os principais concorrentes do anúncio e identifica papéis:
  - líder de vendas (maior sold_quantity)
  - líder orgânico (topo da busca por palavra-chave)
  - mais barato / mais caro
  - referência de qualidade (mais fotos, melhor cadastro)

É executado cedo no pipeline porque quase todos os outros módulos comparam
o anúncio "contra alguém". O resultado (lista de concorrentes) é gravado em
ctx.shared['competitors'] para reuso pelos módulos de preço, título e fotos.

Estratégia de descoberta:
  1. Busca por categoria ordenada por mais vendidos (concorrência real do nicho).
  2. Busca pela query derivada do título (concorrência que o comprador vê).
  3. Deduplica, remove o próprio anúncio, limita a N concorrentes.
"""
from __future__ import annotations

from statistics import mean, median
from typing import Optional

from app.core.config import settings
from app.integrations.normalizer import (
    ItemSnapshot,
    snapshot_from_search_result,
)
from app.modules.base import AnalysisContext, AnalysisModule, Finding, ModuleResult


def _keywords_from_title(title: str, max_terms: int = 6) -> str:
    """Query simples derivada do título (primeiros termos significativos)."""
    stop = {"de", "para", "com", "sem", "e", "o", "a", "os", "as", "em",
            "un", "kit", "novo", "original"}
    terms = [
        t for t in title.lower().split()
        if len(t) > 2 and t not in stop
    ]
    return " ".join(terms[:max_terms])


class BenchmarkModule(AnalysisModule):
    name = "benchmark"

    def run(self, ctx: AnalysisContext) -> ModuleResult:
        item = ctx.item
        competitors: dict[str, ItemSnapshot] = {}

        # 1) concorrentes do nicho (por categoria, mais vendidos)
        if item.category_id:
            by_cat = ctx.client.search_by_category(
                item.category_id,
                limit=settings.benchmark_max_competitors + 5,
            )
            for r in (by_cat or {}).get("results", []):
                snap = snapshot_from_search_result(r)
                if snap.mlb_id and snap.mlb_id != item.mlb_id:
                    competitors[snap.mlb_id] = snap

        # 2) concorrentes que o comprador vê (por palavra-chave do título)
        query = _keywords_from_title(item.title)
        if query:
            by_q = ctx.client.search_by_query(
                query, limit=settings.benchmark_max_competitors + 5
            )
            organic_leader_id: Optional[str] = None
            for idx, r in enumerate((by_q or {}).get("results", [])):
                snap = snapshot_from_search_result(r)
                if not snap.mlb_id or snap.mlb_id == item.mlb_id:
                    continue
                if organic_leader_id is None:
                    organic_leader_id = snap.mlb_id
                competitors.setdefault(snap.mlb_id, snap)
        else:
            organic_leader_id = None

        comp_list = list(competitors.values())[: settings.benchmark_max_competitors]

        result = ModuleResult(module=self.name, score=None)

        if not comp_list:
            result.findings.append(
                Finding(
                    severity="atencao",
                    message="Não foi possível localizar concorrentes comparáveis.",
                    impact_hint="Análises comparativas ficarão limitadas.",
                )
            )
            ctx.shared["competitors"] = []
            return result

        # papéis
        sales_leader = max(
            comp_list, key=lambda c: c.sold_quantity or 0
        )
        prices = [c.price for c in comp_list if c.price is not None]
        cheapest = min(
            (c for c in comp_list if c.price is not None),
            key=lambda c: c.price,
            default=None,
        )

        roles = {
            "lider_vendas": sales_leader.mlb_id,
            "lider_organico": organic_leader_id,
            "mais_barato": cheapest.mlb_id if cheapest else None,
        }

        # métricas agregadas do nicho (reusadas por preço/fotos)
        aggregates = {
            "preco_medio": round(mean(prices), 2) if prices else None,
            "preco_mediano": round(median(prices), 2) if prices else None,
            "preco_min": min(prices) if prices else None,
            "preco_max": max(prices) if prices else None,
            "vendas_lider": sales_leader.sold_quantity,
            "n_concorrentes": len(comp_list),
        }

        result.data = {
            "roles": roles,
            "aggregates": aggregates,
            "competitors": [
                {
                    "mlb_id": c.mlb_id,
                    "title": c.title,
                    "price": c.price,
                    "sold_quantity": c.sold_quantity,
                    "free_shipping": c.free_shipping,
                    "logistic_type": c.logistic_type,
                    "permalink": c.permalink,
                }
                for c in comp_list
            ],
        }

        # compartilha para os próximos módulos
        ctx.shared["competitors"] = comp_list
        ctx.shared["benchmark_aggregates"] = aggregates
        ctx.shared["benchmark_roles"] = roles

        # findings orientados a ação
        if item.sold_quantity is not None and sales_leader.sold_quantity:
            gap = sales_leader.sold_quantity - item.sold_quantity
            if gap > 0:
                result.findings.append(
                    Finding(
                        severity="atencao",
                        message=(
                            f"O líder de vendas do nicho já vendeu "
                            f"{sales_leader.sold_quantity} un., "
                            f"{gap} a mais que este anúncio."
                        ),
                        impact_hint="Referência de teto de vendas no nicho.",
                    )
                )

        return result
