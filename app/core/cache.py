"""
Cache leve baseado em Redis para respostas da API do Mercado Livre.

Objetivo: evitar rate limit e tornar reprocessamentos baratos.
Guarda JSON serializado com TTL configurável.
"""
import json
from typing import Any, Optional

import redis

from app.core.config import settings

_client: Optional[redis.Redis] = None


def get_redis() -> Optional[redis.Redis]:
    global _client
    if _client is None:
        try:
            _client = redis.from_url(settings.redis_url, decode_responses=True)
            _client.ping()
        except Exception:  # Redis ausente -> segue sem cache
            _client = None
    return _client


def cache_get(key: str) -> Optional[Any]:
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def cache_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    client = get_redis()
    if client is None:
        return
    ttl = ttl if ttl is not None else settings.ml_cache_ttl_seconds
    try:
        client.set(key, json.dumps(value), ex=ttl)
    except Exception:
        return
