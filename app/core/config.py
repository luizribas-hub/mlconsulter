"""
Configuração central da aplicação.
Lê variáveis de ambiente (ver .env.example).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "ML Audit"
    environment: str = "development"
    # Modo simples (1 serviço só, sem Redis/Celery): roda a análise em
    # background task da própria API. Ligue USE_CELERY=true para escalar.
    use_celery: bool = False

    # Banco de dados. Padrão SQLite = zero infra externa (bom para hospedar
    # como serviço único). Para escalar, troque por Postgres.
    database_url: str = "sqlite:///./mlaudit.db"

    # Redis (cache + fila)
    redis_url: str = "redis://localhost:6379/0"

    # Mercado Livre
    ml_api_base: str = "https://api.mercadolibre.com"
    ml_site_id: str = "MLB"  # Brasil
    ml_cache_ttl_seconds: int = 1800  # 30 min de cache por item

    # IA (Anthropic)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"

    # Benchmark
    benchmark_max_competitors: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
