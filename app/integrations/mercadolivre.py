"""
Cliente de integração com a API pública do Mercado Livre.

Cobre os dados necessários para o MVP:
  - item (anúncio)
  - descrição do item
  - vendedor (reputação)
  - categoria
  - busca (para posicionamento e benchmark de concorrentes)

Endpoints públicos usados (não exigem OAuth para leitura de dados públicos):
  GET /items/{id}
  GET /items/{id}/description
  GET /users/{id}
  GET /categories/{id}
  GET /sites/{site}/search?q=...
  GET /sites/{site}/search?category=...

Observações importantes:
  - A API do ML muda com frequência; por isso todo acesso passa por
    normalização e degrada com segurança (retorna None em vez de estourar).
  - Todas as respostas são cacheadas em Redis por `mlb_id` / query.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from app.core.cache import cache_get, cache_set
from app.core.config import settings

MLB_ID_RE = re.compile(r"(MLB)-?(\d{6,})", re.IGNORECASE)

class MercadoLivreError(Exception):
    """Erro ao consultar a API do Mercado Livre."""


def normalize_mlb_id(raw: str) -> Optional[str]:
    """
    Aceita 'MLB123456789', 'MLB-123456789' ou um link completo do anúncio
    e devolve o ID normalizado no formato 'MLB123456789'. Retorna None se
    não encontrar um padrão válido.
    """
    if not raw:
        return None
    match = MLB_ID_RE.search(raw.replace(" ", ""))
    if not match:
        return None
    prefix, digits = match.group(1).upper(), match.group(2)
    return f"{prefix}{digits}"


class MercadoLivreClient:
    def __init__(self, timeout: float = 10.0):
        self._client = httpx.Client(
            base_url=settings.ml_api_base,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    # ---- infra interna -------------------------------------------------

    def _get(self, path: str, *, params: dict | None = None,
             cache_key: str | None = None) -> Any:
        if cache_key:
            cached = cache_get(cache_key)
            if cached is not None:
                return cached
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise MercadoLivreError(f"Falha de rede ao chamar {path}: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise MercadoLivreError(
                f"ML respondeu {resp.status_code} em {path}: {resp.text[:200]}"
            )
        data = resp.json()
        if cache_key:
            cache_set(cache_key, data)
        return data

    # ---- endpoints -----------------------------------------------------

    def get_item(self, mlb_id: str) -> Optional[dict]:
        return self._get(f"/items/{mlb_id}", cache_key=f"ml:item:{mlb_id}")

    def get_item_description(self, mlb_id: str) -> Optional[dict]:
        return self._get(
            f"/items/{mlb_id}/description",
            cache_key=f"ml:desc:{mlb_id}",
        )

    def get_user(self, user_id: int | str) -> Optional[dict]:
        return self._get(f"/users/{user_id}", cache_key=f"ml:user:{user_id}")

    def get_category(self, category_id: str) -> Optional[dict]:
        return self._get(
            f"/categories/{category_id}",
            cache_key=f"ml:cat:{category_id}",
        )

    def search_by_query(self, query: str, *, limit: int = 20,
                        offset: int = 0) -> Optional[dict]:
        params = {"q": query, "limit": limit, "offset": offset}
        cache_key = f"ml:search:q:{settings.ml_site_id}:{query}:{limit}:{offset}"
        return self._get(
            f"/sites/{settings.ml_site_id}/search",
            params=params,
            cache_key=cache_key,
        )

    def search_by_category(self, category_id: str, *, limit: int = 20,
                           sort: str = "sold_quantity_desc") -> Optional[dict]:
        params = {"category": category_id, "limit": limit, "sort": sort}
        cache_key = f"ml:search:cat:{category_id}:{limit}:{sort}"
        return self._get(
            f"/sites/{settings.ml_site_id}/search",
            params=params,
            cache_key=cache_key,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MercadoLivreClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
