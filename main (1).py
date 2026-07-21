"""
ML Audit — versão em arquivo único (MVP).

Consultor de otimização de anúncios do Mercado Livre.
Roda como UM serviço só: sem Redis, sem Celery, sem Postgres.
Usa SQLite (arquivo local) e roda a análise em background.

Rodar local:   uvicorn main:app --host 0.0.0.0 --port 8000
Abrir:         http://localhost:8000
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

# ----------------------------------------------------------------------
# Config (via variáveis de ambiente, com padrões seguros)
# ----------------------------------------------------------------------
ML_API_BASE = os.getenv("ML_API_BASE", "https://api.mercadolibre.com")
ML_SITE_ID = os.getenv("ML_SITE_ID", "MLB")
DB_PATH = os.getenv("DB_PATH", "mlaudit.db")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
# Modelo usado para revisar as fotos (pode ser mais barato/rápido que o de texto,
# ex.: claude-sonnet-5 ou claude-haiku-4-5-20251001). Se não setar, usa o mesmo.
ANTHROPIC_VISION_MODEL = os.getenv("ANTHROPIC_VISION_MODEL", ANTHROPIC_MODEL)
BENCHMARK_MAX = int(os.getenv("BENCHMARK_MAX_COMPETITORS", "10"))

# Credenciais do app Mercado Livre (criadas no DevCenter do ML).
# Sem elas o ML bloqueia as chamadas (política nova = exige login).
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID", "")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "")
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI", "")
ML_AUTH_URL = os.getenv("ML_AUTH_URL", "https://auth.mercadolivre.com.br/authorization")

MLB_ID_RE = re.compile(r"(MLB)-?(\d{6,})", re.IGNORECASE)


# ----------------------------------------------------------------------
# Banco (SQLite puro — zero dependências extras)
# ----------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                mlb_id TEXT,
                status TEXT,
                result TEXT,
                error TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                expires_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS competitors_cache (
                part_number TEXT PRIMARY KEY,
                data TEXT,
                updated_at TEXT
            )
            """
        )
        # coluna part_number em analyses (ignora erro se já existir)
        try:
            conn.execute("ALTER TABLE analyses ADD COLUMN part_number TEXT")
        except Exception:
            pass


# ----------------------------------------------------------------------
# OAuth Mercado Livre (guarda tokens e renova sozinho)
# ----------------------------------------------------------------------
def save_tokens(access: str, refresh: str, expires_in: int) -> None:
    exp = time.time() + expires_in - 60  # margem de 60s
    with db() as conn:
        conn.execute("DELETE FROM credentials")
        conn.execute(
            "INSERT INTO credentials (id, access_token, refresh_token, expires_at) "
            "VALUES (1, ?, ?, ?)",
            (access, refresh, exp),
        )


def get_valid_token() -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT * FROM credentials WHERE id=1").fetchone()
    if not row:
        return None
    if time.time() < row["expires_at"]:
        return row["access_token"]
    # token expirou -> renova com o refresh_token
    try:
        r = httpx.post(
            f"{ML_API_BASE}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": ML_CLIENT_ID,
                "client_secret": ML_CLIENT_SECRET,
                "refresh_token": row["refresh_token"],
            },
            timeout=15.0,
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    save_tokens(d["access_token"], d.get("refresh_token", row["refresh_token"]),
                d.get("expires_in", 21600))
    return d["access_token"]


# ----------------------------------------------------------------------
# Integração Mercado Livre
# ----------------------------------------------------------------------
def normalize_mlb_id(raw: str) -> Optional[str]:
    if not raw:
        return None
    m = MLB_ID_RE.search(raw.replace(" ", ""))
    if not m:
        return None
    return f"{m.group(1).upper()}{m.group(2)}"


def ml_get(client: httpx.Client, path: str, params: dict | None = None) -> Any:
    try:
        r = client.get(path, params=params)
    except httpx.HTTPError:
        return None
    if r.status_code == 404 or r.status_code >= 400:
        return None
    return r.json()


# ----------------------------------------------------------------------
# Normalização do item
# ----------------------------------------------------------------------
def snapshot(item: dict, from_search: bool = False) -> dict:
    shipping = item.get("shipping") or {}
    if from_search:
        pics = [item["thumbnail"]] if item.get("thumbnail") else []
        seller_id = (item.get("seller") or {}).get("id")
        attrs: dict = {}
    else:
        pics = [
            p.get("secure_url") or p.get("url")
            for p in item.get("pictures", [])
            if p.get("secure_url") or p.get("url")
        ]
        seller_id = item.get("seller_id")
        attrs = {
            a.get("id"): a.get("value_name")
            for a in item.get("attributes", [])
            if a.get("id")
        }
    return {
        "mlb_id": item.get("id", ""),
        "title": item.get("title", ""),
        "price": item.get("price"),
        "original_price": item.get("original_price"),
        "category_id": item.get("category_id"),
        "listing_type": item.get("listing_type_id"),
        "sold_quantity": item.get("sold_quantity"),
        "available_quantity": item.get("available_quantity"),
        "free_shipping": bool(shipping.get("free_shipping")),
        "logistic_type": shipping.get("logistic_type"),
        "seller_id": seller_id,
        "picture_urls": pics,
        "pictures_count": len(pics),
        "video_id": item.get("video_id"),
        "warranty": item.get("warranty"),
        "health": item.get("health"),
        "catalog_listing": item.get("catalog_listing"),
        "attributes": attrs,
        "attributes_count": len([v for v in attrs.values() if v]),
        "permalink": item.get("permalink"),
        "description": "",  # preenchido depois por /items/{id}/description
    }


# ----------------------------------------------------------------------
# Módulos de análise (scores determinísticos)
# ----------------------------------------------------------------------
def _keywords(title: str, n: int = 6) -> str:
    stop = {"de", "para", "com", "sem", "e", "o", "a", "os", "as", "em",
            "un", "kit", "novo", "original"}
    terms = [t for t in title.lower().split() if len(t) > 2 and t not in stop]
    return " ".join(terms[:n])


def module_benchmark(client: httpx.Client, item: dict, shared: dict) -> dict:
    findings: list[dict] = []
    competitors: dict[str, dict] = {}

    if item.get("category_id"):
        by_cat = ml_get(
            client, f"/sites/{ML_SITE_ID}/search",
            {"category": item["category_id"], "limit": BENCHMARK_MAX + 5,
             "sort": "sold_quantity_desc"},
        )
        for r in (by_cat or {}).get("results", []):
            s = snapshot(r, from_search=True)
            if s["mlb_id"] and s["mlb_id"] != item["mlb_id"]:
                competitors[s["mlb_id"]] = s

    organic_leader = None
    q = _keywords(item.get("title", ""))
    if q:
        by_q = ml_get(client, f"/sites/{ML_SITE_ID}/search",
                      {"q": q, "limit": BENCHMARK_MAX + 5})
        for r in (by_q or {}).get("results", []):
            s = snapshot(r, from_search=True)
            if not s["mlb_id"] or s["mlb_id"] == item["mlb_id"]:
                continue
            if organic_leader is None:
                organic_leader = s["mlb_id"]
            competitors.setdefault(s["mlb_id"], s)

    comp = list(competitors.values())[:BENCHMARK_MAX]
    if not comp:
        findings.append({"severity": "atencao",
                         "message": "Não foi possível localizar concorrentes comparáveis.",
                         "impact": "Comparações ficarão limitadas."})
        shared["aggregates"] = {}
        return {"module": "benchmark", "score": None, "findings": findings, "data": {}}

    prices = [c["price"] for c in comp if c["price"] is not None]
    sales_leader = max(comp, key=lambda c: c["sold_quantity"] or 0)
    agg = {
        "preco_medio": round(mean(prices), 2) if prices else None,
        "preco_mediano": round(median(prices), 2) if prices else None,
        "preco_min": min(prices) if prices else None,
        "preco_max": max(prices) if prices else None,
        "vendas_lider": sales_leader["sold_quantity"],
        "n_concorrentes": len(comp),
    }
    shared["aggregates"] = agg

    if item.get("sold_quantity") is not None and sales_leader["sold_quantity"]:
        gap = sales_leader["sold_quantity"] - item["sold_quantity"]
        if gap > 0:
            findings.append({"severity": "atencao",
                             "message": f"O líder de vendas do nicho já vendeu "
                                        f"{sales_leader['sold_quantity']} un., "
                                        f"{gap} a mais que este anúncio.",
                             "impact": "Referência de teto de vendas no nicho."})

    data = {
        "aggregates": agg,
        "roles": {"lider_vendas": sales_leader["mlb_id"],
                  "lider_organico": organic_leader},
        "competitors": [
            {"mlb_id": c["mlb_id"], "title": c["title"], "price": c["price"],
             "sold_quantity": c["sold_quantity"], "permalink": c["permalink"]}
            for c in comp
        ],
    }
    return {"module": "benchmark", "score": None, "findings": findings, "data": data}


def module_title(item: dict) -> dict:
    """Módulo 6 — SEO do título."""
    findings: list[dict] = []
    title = item.get("title", "") or ""
    n = len(title)
    attrs = item.get("attributes", {})
    has_brand = bool(attrs.get("BRAND"))
    has_model = bool(attrs.get("MODEL") or attrs.get("LINE"))
    first_word_upper = title[:1].isupper()

    score = 0.0
    # aproveitamento do tamanho (títulos do ML vão até ~60 caracteres)
    if n >= 50:
        score += 45
    elif n >= 40:
        score += 35
    elif n >= 25:
        score += 22
    else:
        score += 10
        findings.append({"severity": "atencao",
                         "message": f"Título curto ({n} caracteres) — use até ~60 com palavras-chave.",
                         "impact": "Título completo melhora busca e cliques."})
    # marca e modelo ajudam muito no SEO
    if has_brand:
        score += 20
    else:
        findings.append({"severity": "atencao",
                         "message": "Título/ficha sem a marca preenchida.",
                         "impact": "Marca é uma das palavras mais buscadas."})
    if has_model:
        score += 20
    else:
        findings.append({"severity": "atencao",
                         "message": "Título/ficha sem o modelo/linha preenchido.",
                         "impact": "Modelo específico atrai o comprador certo."})
    if first_word_upper:
        score += 15
    return {"module": "title", "score": round(min(score, 100), 1),
            "findings": findings,
            "data": {"tamanho": n, "tem_marca": has_brand, "tem_modelo": has_model}}


def module_photos(item: dict) -> dict:
    """Módulo 5 (versão enxuta) — quantidade de fotos e vídeo."""
    findings: list[dict] = []
    count = item.get("pictures_count", 0)
    has_video = bool(item.get("video_id"))

    score = min(count / 8.0, 1.0) * 70  # 8+ fotos = teto da parte de fotos
    if count < 4:
        findings.append({"severity": "critico",
                         "message": f"Apenas {count} foto(s). Adicione mais (ideal 6 a 10).",
                         "impact": "Mais fotos aumentam muito a conversão."})
    elif count < 6:
        findings.append({"severity": "atencao",
                         "message": f"{count} fotos. Suba para 6-10 para cobrir ângulos e detalhes.",
                         "impact": "Cobrir mais ângulos reduz dúvidas do comprador."})
    if has_video:
        score += 30
    else:
        findings.append({"severity": "atencao",
                         "message": "Anúncio sem vídeo.",
                         "impact": "Vídeo aumenta confiança e tempo na página."})
    return {"module": "photos", "score": round(min(score, 100), 1),
            "findings": findings,
            "data": {"quantidade": count, "tem_video": has_video}}


def module_description(item: dict) -> dict:
    """Módulo 10 — qualidade da descrição."""
    findings: list[dict] = []
    desc = (item.get("description") or "").strip()
    n = len(desc)
    lines = [l for l in desc.splitlines() if l.strip()]
    structured = len(lines) >= 4  # tem quebras/estrutura

    score = 0.0
    if n >= 800:
        score += 60
    elif n >= 400:
        score += 45
    elif n >= 150:
        score += 25
    else:
        score += 5
        findings.append({"severity": "critico" if n < 50 else "atencao",
                         "message": f"Descrição curta ({n} caracteres). Detalhe benefícios e uso.",
                         "impact": "Descrição rica esclarece dúvidas e vende mais."})
    if structured:
        score += 40
    else:
        findings.append({"severity": "atencao",
                         "message": "Descrição sem estrutura (poucas quebras de linha/tópicos).",
                         "impact": "Texto escaneável facilita a leitura no celular."})
    return {"module": "description", "score": round(min(score, 100), 1),
            "findings": findings,
            "data": {"tamanho": n, "estruturada": structured}}


def module_ficha(item: dict) -> dict:
    """Módulo 15 (parte) — completude da ficha técnica / qualidade do cadastro.
    Usa o índice de saúde (health) que o próprio ML calcula, quando disponível."""
    findings: list[dict] = []
    health = item.get("health")
    attrs_count = item.get("attributes_count", 0)

    if isinstance(health, (int, float)):
        score = round(health * 100, 1)
        if health < 0.8:
            findings.append({"severity": "atencao" if health >= 0.5 else "critico",
                             "message": f"Qualidade do cadastro em {int(health*100)}% "
                                        f"(o ML mede isso). Preencha os campos faltantes.",
                             "impact": "Ficha completa melhora relevância e posicionamento."})
    else:
        # sem health: pontua pela quantidade de atributos preenchidos
        score = min(attrs_count / 12.0, 1.0) * 100
        if attrs_count < 6:
            findings.append({"severity": "atencao",
                             "message": f"Poucos atributos preenchidos ({attrs_count}). "
                                        f"Complete a ficha técnica.",
                             "impact": "Atributos completos melhoram busca e filtros."})
    return {"module": "ficha", "score": round(score, 1), "findings": findings,
            "data": {"health": health, "atributos_preenchidos": attrs_count}}


def module_shipping(item: dict) -> dict:
    """Módulo 8 — frete e logística."""
    findings: list[dict] = []
    free = item.get("free_shipping")
    full = item.get("logistic_type") == "fulfillment"

    score = 0.0
    if free:
        score += 55
    else:
        findings.append({"severity": "atencao",
                         "message": "Sem frete grátis.",
                         "impact": "Frete grátis é forte fator de conversão no ML."})
    if full:
        score += 45
    else:
        findings.append({"severity": "atencao",
                         "message": "Não está no Full (Fulfillment).",
                         "impact": "Full dá entrega rápida e mais destaque na busca."})
    return {"module": "shipping", "score": round(score, 1), "findings": findings,
            "data": {"frete_gratis": bool(free), "full": full}}


def module_price(item: dict, shared: dict) -> dict:
    findings: list[dict] = []
    agg = shared.get("aggregates") or {}
    price, med = item.get("price"), agg.get("preco_mediano")
    if price is None or not med:
        findings.append({"severity": "atencao",
                         "message": "Sem base de preço do nicho para comparar.",
                         "impact": None})
        return {"module": "price", "score": None, "findings": findings, "data": {}}

    diff = round((price - med) / med * 100, 1)
    if diff <= 0:
        score = max(70.0, 100 + diff)
        if diff < -15:
            findings.append({"severity": "atencao",
                             "message": f"Preço {abs(diff)}% abaixo da mediana — verifique margem.",
                             "impact": "Pode estar deixando dinheiro na mesa."})
    else:
        score = max(0.0, 100 - diff * 2)
        sev = "critico" if diff > 15 else "atencao"
        findings.append({"severity": sev,
                         "message": f"Preço {diff}% acima da mediana do nicho.",
                         "impact": "Preço alto reduz conversão frente aos concorrentes."})
    return {"module": "price", "score": round(score, 1), "findings": findings,
            "data": {"preco": price, "mediana_nicho": med, "diff_percentual": diff}}


LEVEL_SCORE = {"platinum": 100, "gold": 85, "silver": 70}


def module_reputation(client: httpx.Client, item: dict) -> dict:
    findings: list[dict] = []
    seller_id = item.get("seller_id")
    if not seller_id:
        findings.append({"severity": "atencao", "message": "Vendedor não identificado.",
                         "impact": None})
        return {"module": "reputation", "score": None, "findings": findings, "data": {}}

    user = ml_get(client, f"/users/{seller_id}") or {}
    rep = user.get("seller_reputation") or {}
    level = rep.get("power_seller_status")
    color = rep.get("level_id")
    metrics = rep.get("metrics") or {}
    base = LEVEL_SCORE.get(level or "", 55.0)

    def rate(k: str) -> float:
        return (metrics.get(k) or {}).get("rate", 0.0) or 0.0

    claims, cancels = rate("claims"), rate("cancellations")
    score = max(0.0, base - (claims + cancels) * 100)

    if color and color.endswith(("red", "orange")):
        findings.append({"severity": "critico",
                         "message": "Reputação do vendedor em faixa de risco (vermelho/laranja).",
                         "impact": "Afeta diretamente a confiança e a conversão."})
    elif not level:
        findings.append({"severity": "atencao",
                         "message": "Vendedor ainda sem selo (Prata/Ouro/Platina).",
                         "impact": "Selo aumenta a confiança do comprador."})
    return {"module": "reputation", "score": round(score, 1), "findings": findings,
            "data": {"power_seller_status": level, "level_id": color,
                     "claims_rate": claims, "cancellations_rate": cancels}}


# ----------------------------------------------------------------------
# Nota geral + classificação
# ----------------------------------------------------------------------
WEIGHTS = {
    "title": 0.16,
    "photos": 0.16,
    "description": 0.12,
    "ficha": 0.14,
    "shipping": 0.10,
    "price": 0.16,
    "reputation": 0.16,
}


def general_score(results: dict[str, dict]) -> Optional[float]:
    num = den = 0.0
    for mod, w in WEIGHTS.items():
        r = results.get(mod)
        if r and r.get("score") is not None:
            num += r["score"] * w
            den += w
    return round(num / den, 1) if den else None


def classify(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 90:
        return "Excelente"
    if score >= 75:
        return "Muito Bom"
    if score >= 60:
        return "Bom"
    if score >= 40:
        return "Regular"
    return "Crítico"


# ----------------------------------------------------------------------
# Consultor (plano de ação + resumo)
# ----------------------------------------------------------------------
SEV_TO_PRIO = {"critico": "alta", "atencao": "media", "positivo": "baixa"}


def build_plan(results: dict[str, dict]) -> list[dict]:
    findings = []
    for r in results.values():
        for f in r.get("findings", []):
            findings.append(f)
    order = {"critico": 0, "atencao": 1, "positivo": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    plan = []
    for f in findings:
        if f["severity"] == "positivo":
            continue
        plan.append({"priority": SEV_TO_PRIO.get(f["severity"], "media"),
                     "title": f["message"], "impact": f.get("impact")})
    return plan


def _ai_client():
    """Cliente Anthropic, ou None se não houver chave/lib disponível."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic  # import tardio: só se houver chave
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception:
        return None


def _parse_json_response(text: str) -> Optional[dict]:
    """A IA às vezes responde com ```json ... ``` em volta — limpa antes de parsear."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        return json.loads(text)
    except Exception:
        return None


def _fallback_summary(item: dict, score: Optional[float], results: dict[str, dict],
                       findings: list[dict]) -> str:
    """Resumo determinístico — usado quando não há IA ou a chamada falha."""
    nota = f"{score}/100" if score is not None else "ainda não pontuável"
    criticos = [f["message"] for f in findings if f["severity"] == "critico"]
    atencao = [f["message"] for f in findings if f["severity"] == "atencao"]
    notas = [(r["module"], r["score"]) for r in results.values()
             if r.get("score") is not None]
    notas.sort(key=lambda x: x[1])
    nomes = {"title": "título", "photos": "fotos", "description": "descrição",
             "ficha": "ficha técnica", "shipping": "frete", "price": "preço",
             "reputation": "reputação"}
    piores = ", ".join(f"{nomes.get(m, m)} ({s:.0f})" for m, s in notas[:3])
    partes = [f"Análise de '{item.get('title')}'. Nota geral: {nota}."]
    if piores:
        partes.append(f"Onde focar primeiro (notas mais baixas): {piores}.")
    if criticos:
        partes.append("Pontos críticos: " + "; ".join(criticos[:3]) + ".")
    elif atencao:
        partes.append("Principais melhorias: " + "; ".join(atencao[:3]) + ".")
    partes.append("Siga o plano de ação abaixo na ordem de prioridade. "
                  "(Para uma análise ainda mais detalhada e personalizada, "
                  "configure a chave de IA no servidor.)")
    return " ".join(partes)


def build_ai_content(item: dict, score: Optional[float],
                      results: dict[str, dict]) -> dict:
    """Resumo do consultor + título/descrição sugeridos.
    Usa a IA da Anthropic quando ANTHROPIC_API_KEY está configurada;
    sempre cai para o resumo determinístico se a IA falhar ou não estiver ativa."""
    findings = [f for r in results.values() for f in r.get("findings", [])
                if f["severity"] != "positivo"]
    out = {
        "consultant_summary": _fallback_summary(item, score, results, findings),
        "titulo_sugerido": None,
        "descricao_sugerida": None,
        "ai_powered": False,
    }
    client = _ai_client()
    if not client:
        return out

    payload = {
        "titulo_atual": item.get("title"),
        "preco": item.get("price"),
        "nota_geral": score,
        "notas_por_modulo": {r["module"]: r["score"] for r in results.values()
                             if r.get("score") is not None},
        "concorrentes": results.get("benchmark", {}).get("data", {}).get("aggregates"),
        "descricao_atual": (item.get("description") or "")[:1500],
        "achados": findings,
    }
    prompt = (
        "Você é um consultor especialista em otimização de anúncios do Mercado "
        "Livre. Com base SOMENTE nos dados estruturados abaixo, responda em JSON "
        "puro (sem markdown, sem texto fora do JSON), com exatamente estas chaves:\n"
        '"resumo": string — o que você faria se este anúncio fosse seu, direto e '
        "focado em vendas, sem inventar métricas que não estão nos dados;\n"
        '"titulo_otimizado": um título novo de até 60 caracteres seguindo boas '
        "práticas de SEO do Mercado Livre (marca + modelo + atributo-chave), ou "
        "null se o título atual já estiver ótimo;\n"
        '"descricao_otimizada": uma descrição nova, estruturada e persuasiva '
        "(300 a 700 caracteres), ou null se a atual já for boa.\n\n"
        f"DADOS:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    try:
        msg = client.messages.create(model=ANTHROPIC_MODEL, max_tokens=1200,
                                      messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = _parse_json_response(text)
        if data:
            out["consultant_summary"] = data.get("resumo") or out["consultant_summary"]
            out["titulo_sugerido"] = data.get("titulo_otimizado") or None
            out["descricao_sugerida"] = data.get("descricao_otimizada") or None
            out["ai_powered"] = True
    except Exception:
        pass  # mantém o fallback determinístico já preenchido em out
    return out


def module_photo_review(item: dict) -> dict:
    """Módulo de IA (visão) — revisa as fotos do anúncio e aponta o que trocar
    (enquadramento, fundo, iluminação, ângulos faltando). Não gera nota (score
    None) — é consultivo e entra no plano de ação junto com os demais módulos."""
    findings: list[dict] = []
    empty_data = {"ativo": False, "comentarios": [], "prioridade": None}
    pics = (item.get("picture_urls") or [])[:3]
    client = _ai_client()
    if not client or not pics:
        return {"module": "photo_review", "score": None, "findings": findings,
                "data": empty_data}

    content: list[dict] = [{
        "type": "text",
        "text": ("Você é um especialista em fotografia de produto para anúncios do "
                 "Mercado Livre. Analise estas fotos (na ordem em que aparecem no "
                 "anúncio) e responda em JSON puro, sem markdown, com as chaves:\n"
                 '"comentarios": lista com uma string por foto analisada, apontando '
                 "o problema/melhoria mais importante de cada uma;\n"
                 '"prioridade": uma frase única com a ação mais importante a fazer '
                 "nas fotos deste anúncio."),
    }]
    import base64
    for url in pics:
        try:
            img = httpx.get(url, timeout=10.0)
            if img.status_code == 200 and img.content:
                media_type = img.headers.get("content-type", "image/jpeg").split(";")[0]
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type,
                              "data": base64.b64encode(img.content).decode()},
                })
        except Exception:
            continue
    if len(content) == 1:  # nenhuma foto baixou
        return {"module": "photo_review", "score": None, "findings": findings,
                "data": empty_data}

    try:
        msg = client.messages.create(model=ANTHROPIC_VISION_MODEL, max_tokens=600,
                                      messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = _parse_json_response(text) or {}
        comentarios = data.get("comentarios") or []
        prioridade = data.get("prioridade")
        if prioridade:
            findings.append({"severity": "atencao", "message": prioridade,
                             "impact": "Fotos melhores aumentam diretamente a conversão."})
        return {"module": "photo_review", "score": None, "findings": findings,
                "data": {"ativo": True, "comentarios": comentarios,
                         "prioridade": prioridade}}
    except Exception:
        return {"module": "photo_review", "score": None, "findings": findings,
                "data": empty_data}


# ----------------------------------------------------------------------
# Orquestração (roda em background)
# ----------------------------------------------------------------------
def run_analysis(analysis_id: str, mlb_id: str) -> None:
    try:
        token = get_valid_token()
        if not token:
            raise RuntimeError(
                "O app ainda não está conectado ao Mercado Livre. "
                "Clique em 'Conectar com Mercado Livre' na página inicial."
            )
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=ML_API_BASE, timeout=15.0, headers=headers) as client:
            resp = client.get(f"/items/{mlb_id}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"ML respondeu {resp.status_code} ao ler {mlb_id}: "
                    f"{resp.text[:200]}"
                )
            item = snapshot(resp.json())
            # descrição (agora temos permissão de leitura)
            desc = ml_get(client, f"/items/{mlb_id}/description")
            if desc:
                item["description"] = desc.get("plain_text") or desc.get("text") or ""
            # número da peça / OEM (chave para achar concorrentes exatos)
            attrs = item.get("attributes", {})
            part_number = (attrs.get("PART_NUMBER") or attrs.get("OEM")
                           or attrs.get("MPN") or attrs.get("ALTERNATOR_PART_NUMBER")
                           or "")
            part_number = re.sub(r"\s+", "", str(part_number)) if part_number else ""
            item["part_number"] = part_number
            shared: dict = {}
            results = {}
            results["benchmark"] = module_benchmark(client, item, shared)
            results["title"] = module_title(item)
            results["photos"] = module_photos(item)
            results["description"] = module_description(item)
            results["ficha"] = module_ficha(item)
            results["shipping"] = module_shipping(item)
            results["price"] = module_price(item, shared)
            results["reputation"] = module_reputation(client, item)

        # módulo de IA (visão) — não depende do client autenticado do ML
        results["photo_review"] = module_photo_review(item)

        score = general_score(results)
        ai = build_ai_content(item, score, results)
        result = {
            "general_score": score,
            "classification": classify(score),
            "consultant_summary": ai["consultant_summary"],
            "ai_powered": ai["ai_powered"],
            "titulo_sugerido": ai["titulo_sugerido"],
            "descricao_sugerida": ai["descricao_sugerida"],
            "photo_review": results["photo_review"]["data"],
            "part_number": item.get("part_number", ""),
            "meu_preco": item.get("price"),
            "meu_titulo": item.get("title"),
            "minhas_fotos": item.get("pictures_count"),
            "scores": [{"module": r["module"], "score": r["score"],
                        "details": r["data"]} for r in results.values()],
            "action_items": build_plan(results),
        }
        with db() as conn:
            conn.execute("UPDATE analyses SET status=?, result=?, part_number=? WHERE id=?",
                         ("done", json.dumps(result, ensure_ascii=False),
                          item.get("part_number", ""), analysis_id))
    except Exception as exc:  # noqa: BLE001
        with db() as conn:
            conn.execute("UPDATE analyses SET status=?, error=? WHERE id=?",
                         ("failed", str(exc)[:500], analysis_id))


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
class CreateReq(BaseModel):
    input: str


app = FastAPI(title="ML Audit")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def api_status() -> dict:
    return {
        "connected": get_valid_token() is not None,
        "configured": bool(ML_CLIENT_ID and ML_CLIENT_SECRET and ML_REDIRECT_URI),
    }


BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}


@app.get("/api/debug/search")
def debug_search(q: str = "filtro de ar") -> dict:
    """Diagnóstico: testa a busca via API (autenticada) e a página pública."""
    out: dict = {"query": q}
    token = get_valid_token()
    # 1) busca via API oficial (autenticada)
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = httpx.get(f"{ML_API_BASE}/sites/{ML_SITE_ID}/search",
                      params={"q": q, "limit": 5}, headers=headers, timeout=15.0)
        out["api_status"] = r.status_code
        try:
            out["api_count"] = len(r.json().get("results", []))
        except Exception:
            out["api_body"] = r.text[:200]
    except Exception as e:  # noqa: BLE001
        out["api_error"] = str(e)[:200]
    # 2) página pública do site (fingindo navegador)
    try:
        slug = q.strip().replace(" ", "-")
        r2 = httpx.get(f"https://lista.mercadolivre.com.br/{slug}",
                       headers=BROWSER_HEADERS, timeout=20.0, follow_redirects=True)
        html = r2.text
        out["public_status"] = r2.status_code
        out["public_len"] = len(html)
        out["public_url_final"] = str(r2.url)[:120]
        out["marker_result"] = "ui-search-result" in html or "poly-component" in html
        out["marker_preco"] = "andes-money-amount__fraction" in html
        low = html.lower()
        out["parece_bloqueio"] = any(w in low for w in
                                     ["captcha", "robot", "acesso negado",
                                      "access denied", "unusual traffic"])
        out["trecho"] = html[:200]
    except Exception as e:  # noqa: BLE001
        out["public_error"] = str(e)[:200]
    return out


@app.get("/oauth/login")
def oauth_login() -> RedirectResponse:
    url = (f"{ML_AUTH_URL}?response_type=code&client_id={ML_CLIENT_ID}"
           f"&redirect_uri={ML_REDIRECT_URI}")
    return RedirectResponse(url)


@app.get("/oauth/callback")
def oauth_callback(code: str = "") -> RedirectResponse:
    if not code:
        return RedirectResponse("/?connected=0")
    try:
        r = httpx.post(
            f"{ML_API_BASE}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": ML_CLIENT_ID,
                "client_secret": ML_CLIENT_SECRET,
                "code": code,
                "redirect_uri": ML_REDIRECT_URI,
            },
            timeout=15.0,
        )
    except httpx.HTTPError:
        return RedirectResponse("/?connected=0")
    if r.status_code != 200:
        return RedirectResponse("/?connected=0")
    d = r.json()
    save_tokens(d["access_token"], d.get("refresh_token", ""),
                d.get("expires_in", 21600))
    return RedirectResponse("/?connected=1")


@app.post("/api/analysis")
def create_analysis(body: CreateReq, background: BackgroundTasks) -> dict:
    mlb_id = normalize_mlb_id(body.input)
    if not mlb_id:
        raise HTTPException(422, "Informe um código MLB válido ou o link do anúncio.")
    analysis_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO analyses (id, mlb_id, status, created_at) VALUES (?,?,?,?)",
            (analysis_id, mlb_id, "pending", datetime.now(timezone.utc).isoformat()),
        )
    background.add_task(run_analysis, analysis_id, mlb_id)
    return {"analysis_id": analysis_id, "status": "pending"}


@app.get("/api/analysis/{analysis_id}")
def get_analysis(analysis_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Análise não encontrada.")
    out = {"analysis_id": row["id"], "mlb_id": row["mlb_id"],
           "status": row["status"], "error": row["error"]}
    if row["result"]:
        out.update(json.loads(row["result"]))
    # anexa concorrentes coletados (Opção B) e calcula o comparativo
    pn = row["part_number"] if "part_number" in row.keys() else None
    if pn:
        comp = load_competitors(pn)
        if comp:
            out["competitors"] = comp
            out["market"] = market_stats(comp, out.get("meu_preco"))
    return out


class CompetitorsPayload(BaseModel):
    part_number: str
    competitors: list[dict]


@app.post("/api/competitors")
def save_competitors(body: CompetitorsPayload) -> dict:
    pn = re.sub(r"\s+", "", body.part_number or "")
    if not pn:
        raise HTTPException(422, "part_number vazio.")
    # limpa/normaliza os itens recebidos do navegador
    clean = []
    for c in body.competitors[:40]:
        clean.append({
            "title": (c.get("title") or "")[:200],
            "price": c.get("price"),
            "sold": c.get("sold"),
            "rating": c.get("rating"),
            "reviews": c.get("reviews"),
            "full": bool(c.get("full")),
            "free_shipping": bool(c.get("free_shipping")),
            "permalink": (c.get("permalink") or "")[:400],
            "picture": (c.get("picture") or "")[:400],
        })
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO competitors_cache (part_number, data, updated_at) "
            "VALUES (?,?,?)",
            (pn, json.dumps(clean, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()),
        )
    return {"ok": True, "part_number": pn, "recebidos": len(clean)}


def load_competitors(part_number: str) -> list[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT data FROM competitors_cache WHERE part_number=?",
            (part_number,),
        ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["data"])
    except Exception:
        return []


def market_stats(comp: list[dict], meu_preco: Optional[float]) -> dict:
    precos = [c["price"] for c in comp if isinstance(c.get("price"), (int, float))]
    vendas = [c["sold"] for c in comp if isinstance(c.get("sold"), (int, float))]
    ratings = [c["rating"] for c in comp if isinstance(c.get("rating"), (int, float))]
    stats: dict = {"n": len(comp)}
    if precos:
        stats["preco_min"] = round(min(precos), 2)
        stats["preco_mediano"] = round(median(precos), 2)
        stats["preco_medio"] = round(mean(precos), 2)
        stats["preco_max"] = round(max(precos), 2)
    if vendas:
        stats["vendas_media"] = round(mean(vendas), 1)
        stats["vendas_lider"] = max(vendas)
    if ratings:
        stats["avaliacao_media"] = round(mean(ratings), 2)
    stats["com_full"] = sum(1 for c in comp if c.get("full"))
    stats["com_frete_gratis"] = sum(1 for c in comp if c.get("free_shipping"))
    # posicionamento do seu preço
    if isinstance(meu_preco, (int, float)) and precos:
        mais_baratos = sum(1 for p in precos if p < meu_preco)
        stats["seu_preco"] = meu_preco
        stats["mais_baratos_que_voce"] = mais_baratos
        stats["posicao_preco"] = f"{mais_baratos+1}º mais caro de {len(precos)+1}"
        med = stats.get("preco_mediano")
        if med:
            diff = round((meu_preco - med) / med * 100, 1)
            stats["diff_mediana_pct"] = diff
    return stats


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


# ----------------------------------------------------------------------
# Frontend (tela embutida)
# ----------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Consultor de Anúncios — Mercado Livre</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f6f8;color:#1a1a1a}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px 64px}h1{font-size:22px;margin:0 0 4px}
p.sub{color:#666;margin:0 0 24px;font-size:14px}
.card{background:#fff;border:1px solid #e6e6e6;border-radius:12px;padding:20px;margin-bottom:16px}
.row{display:flex;gap:8px}input{flex:1;padding:12px 14px;font-size:15px;border:1px solid #ccc;border-radius:8px}
button{padding:12px 20px;font-size:15px;font-weight:600;border:0;border-radius:8px;background:#3483fa;color:#fff;cursor:pointer}
button:disabled{opacity:.5}.score{font-size:44px;font-weight:700}.classif{font-size:15px;color:#555}
.muted{color:#888;font-size:13px}.item{padding:12px 0;border-bottom:1px solid #eee}.item:last-child{border-bottom:0}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-right:8px;text-transform:uppercase}
.alta{background:#ffe3e3;color:#c92a2a}.media{background:#fff3bf;color:#a37200}.baixa{background:#e6fcf5;color:#087f5b}
.hidden{display:none}pre.summary{white-space:pre-wrap;font-family:inherit;font-size:14px;line-height:1.5;margin:0}
</style></head><body><div class="wrap">
<h1>Consultor de Anúncios — Mercado Livre</h1>
<p class="sub">Cole o código MLB ou o link do anúncio e receba um plano de ação.</p>
<div id="conn" class="muted" style="margin-bottom:16px"></div>
<div class="card"><div class="row">
<input id="mlb" placeholder="Ex.: MLB123456789 ou link do anúncio"/>
<button id="go">Analisar</button></div>
<p id="status" class="muted" style="margin:12px 0 0"></p></div>
<div id="result" class="hidden">
<div class="card"><div class="score" id="score">–</div><div class="classif" id="classif"></div></div>
<div class="card"><strong>Notas por área</strong><div id="scores" style="margin-top:8px"></div></div>
<div class="card"><strong>O que eu faria se fosse meu</strong> <span id="aibadge" class="muted"></span>
<pre class="summary" id="summary" style="margin-top:10px"></pre></div>
<div class="card hidden" id="aicard"><strong>Sugestões de IA para o anúncio</strong>
<div id="aititulo" style="margin-top:10px"></div>
<div id="aidesc" style="margin-top:10px"></div></div>
<div class="card hidden" id="photocard"><strong>Revisão das fotos (IA)</strong>
<div id="photorev" style="margin-top:10px"></div></div>
<div class="card"><strong>Plano de ação</strong><div id="actions" style="margin-top:8px"></div></div>
<div class="card" id="compcard"><strong>Concorrência por número de peça</strong>
<div id="compaction" style="margin-top:10px"></div>
<div id="market" style="margin-top:10px"></div>
<div id="comp" style="margin-top:10px"></div></div>
</div></div>
<script>
window.APP_BASE=location.origin;
const $=(i)=>document.getElementById(i);const btn=$("go"),input=$("mlb"),st=$("status");
async function start(){const v=input.value.trim();if(!v)return;btn.disabled=true;
$("result").classList.add("hidden");st.textContent="Criando análise...";
try{const r=await fetch("/api/analysis",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({input:v})});
if(!r.ok){st.textContent="Código inválido.";btn.disabled=false;return;}
const {analysis_id}=await r.json();poll(analysis_id);}
catch(e){st.textContent="Erro de conexão.";btn.disabled=false;}}
async function poll(id){st.textContent="Analisando o anúncio...";
for(let i=0;i<40;i++){await new Promise(x=>setTimeout(x,1500));
const r=await fetch("/api/analysis/"+id);const d=await r.json();
if(d.status==="done"){render(d);return;}
if(d.status==="failed"){st.textContent="Falhou: "+(d.error||"erro");btn.disabled=false;return;}}
st.textContent="Tempo esgotado. Tente de novo.";btn.disabled=false;}
let CUR={id:null,pn:""};
function render(d){st.textContent="";btn.disabled=false;$("result").classList.remove("hidden");
CUR.id=d.analysis_id;CUR.pn=d.part_number||"";
$("score").textContent=(d.general_score??"–")+(d.general_score!=null?"/100":"");
$("classif").textContent=d.classification||"";$("summary").textContent=d.consultant_summary||"";
$("aibadge").textContent=d.ai_powered?"\u2728 gerado por IA":"(sem IA configurada — resumo por regras)";
renderAiCard(d);renderPhotoCard(d);
const nomes={title:"Título",photos:"Fotos",description:"Descrição",ficha:"Ficha técnica",shipping:"Frete",price:"Preço",reputation:"Reputação",benchmark:"Concorrência"};
const sc=$("scores");sc.innerHTML="";
(d.scores||[]).forEach(s=>{if(s.score==null)return;
const cor=s.score>=75?"#087f5b":s.score>=50?"#a37200":"#c92a2a";
const row=document.createElement("div");row.className="item";
row.innerHTML='<div style="display:flex;justify-content:space-between"><span>'+(nomes[s.module]||s.module)+'</span><strong style="color:'+cor+'">'+s.score+'</strong></div>';
sc.appendChild(row);});
const b=$("actions");b.innerHTML="";
if(!d.action_items||!d.action_items.length){b.innerHTML='<p class="muted">Nenhuma ação prioritária.</p>';}
else{for(const a of d.action_items){const div=document.createElement("div");div.className="item";
const im=a.impact?'<div class="muted">'+a.impact+'</div>':'';
div.innerHTML='<span class="tag '+a.priority+'">'+a.priority+'</span>'+a.title+im;b.appendChild(div);}}
renderComp(d);}
function copyBtn(text){const b=document.createElement("button");b.textContent="Copiar";
b.style.cssText="padding:4px 10px;font-size:12px;font-weight:600;margin-left:8px;background:#eef4ff;color:#3483fa";
b.addEventListener("click",function(){navigator.clipboard.writeText(text).then(function(){
b.textContent="Copiado!";setTimeout(function(){b.textContent="Copiar";},1500);});});return b;}
function renderAiCard(d){const card=$("aicard"),t=$("aititulo"),ds=$("aidesc");t.innerHTML="";ds.innerHTML="";
if(!d.titulo_sugerido&&!d.descricao_sugerida){card.classList.add("hidden");return;}
card.classList.remove("hidden");
if(d.titulo_sugerido){const p=document.createElement("p");p.innerHTML="<b>Título sugerido:</b><br>"+d.titulo_sugerido;
p.appendChild(copyBtn(d.titulo_sugerido));t.appendChild(p);}
if(d.descricao_sugerida){const p=document.createElement("p");p.style.whiteSpace="pre-wrap";
p.innerHTML="<b>Descrição sugerida:</b><br>"+d.descricao_sugerida;
p.appendChild(copyBtn(d.descricao_sugerida));ds.appendChild(p);}}
function renderPhotoCard(d){const card=$("photocard"),box=$("photorev");box.innerHTML="";
const pr=d.photo_review;
if(!pr||!pr.ativo||!(pr.comentarios&&pr.comentarios.length)){card.classList.add("hidden");return;}
card.classList.remove("hidden");
if(pr.prioridade){const p=document.createElement("p");p.innerHTML="<b>Prioridade:</b> "+pr.prioridade;box.appendChild(p);}
pr.comentarios.forEach(function(c,i){const d2=document.createElement("div");d2.className="item";
d2.innerHTML="<b>Foto "+(i+1)+":</b> "+c;box.appendChild(d2);});}
function bookmarklet(){var base=window.APP_BASE;
return "javascript:(function(){var m=location.href.match(/[0-9]{5,}/);var pn=m?m[0]:prompt('Numero da peca?');var cards=document.querySelectorAll('li.ui-search-layout__item, div.ui-search-result, .poly-card');var out=[];cards.forEach(function(el){var t=el.querySelector('a.poly-component__title, .ui-search-item__title, h2 a, h3 a');var title=t?t.innerText.trim():'';var link=t&&t.href?t.href:'';var fr=el.querySelector('.andes-money-amount__fraction');var price=fr?parseInt(fr.innerText.replace(/[^0-9]/g,'')):null;var txt=el.innerText;var i=txt.toLowerCase().indexOf('vendid');var sold=null;if(i>-1){var pre=txt.slice(Math.max(0,i-12),i).replace(/[^0-9]/g,'');if(pre)sold=parseInt(pre);}var rm=txt.match(/[0-9],[0-9]/);var rating=rm?parseFloat(rm[0].replace(',','.')):null;var full=/full/i.test(txt);var free=/gr[a\u00e1]tis/i.test(txt);var img=el.querySelector('img');var pic=img?(img.getAttribute('data-src')||img.src):'';if(title&&price!=null)out.push({title:title,price:price,sold:sold,rating:rating,full:full,free_shipping:free,permalink:link,picture:pic});});if(!out.length){alert('Nao encontrei produtos nesta pagina. Abra a lista de resultados do Mercado Livre e tente de novo.');return;}fetch('"+base+"/api/competitors',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({part_number:pn,competitors:out})}).then(function(r){return r.json();}).then(function(d){alert('Enviei '+d.recebidos+' concorrentes para a ferramenta. Volte e clique em Atualizar comparativo.');}).catch(function(e){alert('Erro ao enviar: '+e);});})();";}
function renderComp(d){const ca=$("compaction"),mk=$("market"),cd=$("comp");ca.innerHTML="";mk.innerHTML="";cd.innerHTML="";
if(!CUR.pn){ca.innerHTML='<p class="muted">Para comparar com concorrentes, preencha o campo <b>Número de peça</b> ou <b>Código OEM</b> na ficha técnica do seu anúncio no Mercado Livre.</p>';return;}
const url="https://lista.mercadolivre.com.br/"+encodeURIComponent(CUR.pn);
ca.innerHTML='<p class="muted">Número da peça detectado: <b>'+CUR.pn+'</b></p>'
+'<p style="margin:6px 0"><b>1.</b> <a href="'+url+'" target="_blank" style="color:#3483fa;font-weight:600">Abrir concorrentes no Mercado Livre</a></p>';
const p2=document.createElement("p");p2.style.margin="6px 0";
p2.innerHTML='<b>2.</b> Arraste este botão para a barra de favoritos (só na 1ª vez): ';
const bm=document.createElement("a");bm.textContent="Coletar concorrentes";
bm.setAttribute("href",bookmarklet());bm.style.color="#087f5b";bm.style.fontWeight="700";
p2.appendChild(bm);ca.appendChild(p2);
const p3=document.createElement("p");p3.style.margin="10px 0 0";p3.innerHTML="<b>3.</b> ";
const rb=document.createElement("button");rb.textContent="Atualizar comparativo";rb.style.padding="8px 14px";
p3.appendChild(rb);ca.appendChild(p3);
rb.addEventListener("click",async function(){rb.disabled=true;rb.textContent="Atualizando...";
const r=await fetch("/api/analysis/"+CUR.id);const nd=await r.json();render(nd);});
const m=d.market,comps=d.competitors||[];
if(m&&m.n){let h='<div class="item"><strong>Panorama do mercado ('+m.n+' concorrentes)</strong></div>';
if(m.preco_mediano!=null)h+='<div class="item">Preço: mín R$'+m.preco_min+' · mediano R$'+m.preco_mediano+' · máx R$'+m.preco_max+'</div>';
if(m.seu_preco!=null&&m.posicao_preco)h+='<div class="item">Seu preço: R$'+m.seu_preco+' — <b>'+m.posicao_preco+'</b>'+(m.diff_mediana_pct!=null?' ('+(m.diff_mediana_pct>0?'+':'')+m.diff_mediana_pct+'% vs mediana)':'')+'</div>';
if(m.vendas_lider!=null)h+='<div class="item">Vendas: líder '+m.vendas_lider+' · média '+m.vendas_media+'</div>';
if(m.avaliacao_media!=null)h+='<div class="item">Avaliação média do nicho: '+m.avaliacao_media+'</div>';
h+='<div class="item muted">Com Full: '+m.com_full+' · Com frete grátis: '+m.com_frete_gratis+'</div>';
mk.innerHTML=h;}
comps.slice(0,10).forEach(c=>{const div=document.createElement("div");div.className="item";
let extra=[];if(c.sold!=null)extra.push(c.sold+' vendidos');if(c.rating!=null)extra.push('★'+c.rating);if(c.full)extra.push('FULL');if(c.free_shipping)extra.push('frete grátis');
div.innerHTML='<a href="'+(c.permalink||"#")+'" target="_blank" style="color:#3483fa">'+c.title+'</a><div class="muted">R$ '+(c.price??"?")+(extra.length?' · '+extra.join(' · '):'')+'</div>';cd.appendChild(div);});}
btn.addEventListener("click",start);input.addEventListener("keydown",e=>{if(e.key==="Enter")start();});
async function checkStatus(){try{const r=await fetch('/api/status');const s=await r.json();
const c=document.getElementById('conn');
if(s.connected){c.innerHTML='\u2705 Conectado ao Mercado Livre';}
else if(!s.configured){c.innerHTML='\u26a0\ufe0f Falta configurar as credenciais do Mercado Livre no servidor (ML_CLIENT_ID, ML_CLIENT_SECRET, ML_REDIRECT_URI).';}
else{c.innerHTML='<a href="/oauth/login" style="color:#3483fa;font-weight:600">&#x1F517; Conectar com Mercado Livre</a> (necessário antes de analisar)';}
}catch(e){}}
checkStatus();
</script></body></html>"""
