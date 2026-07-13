"""
Contrato base de um módulo de análise.

Todo módulo:
  - recebe um AnalysisContext (snapshot do item + acesso ao client + resultados
    de módulos anteriores)
  - devolve um ModuleResult (nota determinística + achados estruturados)

A nota é SEMPRE calculada por regras aqui (determinístico e auditável).
A IA generativa entra depois, só na camada de síntese/consultor — ela
explica e prioriza, não inventa números.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.integrations.mercadolivre import MercadoLivreClient
from app.integrations.normalizer import ItemSnapshot


@dataclass
class AnalysisContext:
    item: ItemSnapshot
    client: MercadoLivreClient
    # resultados de módulos já executados, indexados pelo nome do módulo
    results: dict[str, "ModuleResult"] = field(default_factory=dict)
    # dados auxiliares compartilhados (ex.: lista de concorrentes)
    shared: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """Um achado específico dentro de um módulo."""
    severity: str          # "positivo" | "atencao" | "critico"
    message: str
    impact_hint: Optional[str] = None   # dica de impacto para o consultor


@dataclass
class ModuleResult:
    module: str
    score: Optional[float]              # 0..100 (None quando não aplicável)
    findings: list[Finding] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)  # payload -> JSONB

    def as_dict(self) -> dict:
        return {
            "module": self.module,
            "score": self.score,
            "findings": [f.__dict__ for f in self.findings],
            "data": self.data,
        }


class AnalysisModule:
    name: str = "base"

    def run(self, ctx: AnalysisContext) -> ModuleResult:  # pragma: no cover
        raise NotImplementedError
