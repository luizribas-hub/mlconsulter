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
        "category_id": item.get("category_id"),
        "listing_type": item.get("listing_type_id"),
        "sold_quantity": item.get("sold_quantity"),
        "free_shipping": bool(shipping.get("free_shipping")),
        "logistic_type": shipping.get("logistic_type"),
        "seller_id": seller_id,
        "picture_urls": pics,
        "attributes": attrs,
        "permalink": item.get("permalink"),
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
WEIGHTS = {"price": 0.55, "reputation": 0.45}


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


def build_summary(item: dict, score: Optional[float], results: dict[str, dict]) -> str:
    findings = [f for r in results.values() for f in r.get("findings", [])
                if f["severity"] != "positivo"]
    if ANTHROPIC_API_KEY:
        try:
            import anthropic  # import tardio: só se houver chave
            payload = {"titulo": item.get("title"), "preco": item.get("price"),
                       "nota_geral": score,
                       "concorrentes": results.get("benchmark", {}).get("data", {}).get("aggregates"),
                       "achados": findings}
            prompt = ("Você é um consultor especialista em otimização de anúncios do "
                      "Mercado Livre. Com base SOMENTE nos dados estruturados abaixo, "
                      "responda: 'o que eu faria se este anúncio fosse meu?'. Seja direto, "
                      "foque em aumento de vendas, não invente métricas.\n\n"
                      f"DADOS:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(model=ANTHROPIC_MODEL, max_tokens=800,
                                          messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        except Exception:
            pass
    nota = f"{score}/100" if score is not None else "ainda não pontuável"
    return (f"Análise do anúncio '{item.get('title')}'. Nota geral: {nota}. "
            f"Foram identificados {len(findings)} pontos de melhoria no plano de ação. "
            f"(Configure ANTHROPIC_API_KEY para a narrativa completa do consultor.)")


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
            item_raw = ml_get(client, f"/items/{mlb_id}")
            if not item_raw:
                raise RuntimeError(f"Anúncio {mlb_id} não encontrado ou não acessível.")
            item = snapshot(item_raw)
            shared: dict = {}
            results = {}
            results["benchmark"] = module_benchmark(client, item, shared)
            results["price"] = module_price(item, shared)
            results["reputation"] = module_reputation(client, item)

        score = general_score(results)
        result = {
            "general_score": score,
            "classification": classify(score),
            "consultant_summary": build_summary(item, score, results),
            "scores": [{"module": r["module"], "score": r["score"],
                        "details": r["data"]} for r in results.values()],
            "action_items": build_plan(results),
        }
        with db() as conn:
            conn.execute("UPDATE analyses SET status=?, result=? WHERE id=?",
                         ("done", json.dumps(result, ensure_ascii=False), analysis_id))
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
    return out


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
<div class="card"><strong>O que eu faria se fosse meu</strong>
<pre class="summary" id="summary" style="margin-top:10px"></pre></div>
<div class="card"><strong>Plano de ação</strong><div id="actions" style="margin-top:8px"></div></div>
</div></div>
<script>
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
function render(d){st.textContent="";btn.disabled=false;$("result").classList.remove("hidden");
$("score").textContent=(d.general_score??"–")+(d.general_score!=null?"/100":"");
$("classif").textContent=d.classification||"";$("summary").textContent=d.consultant_summary||"";
const b=$("actions");b.innerHTML="";
if(!d.action_items||!d.action_items.length){b.innerHTML='<p class="muted">Nenhuma ação prioritária.</p>';return;}
for(const a of d.action_items){const div=document.createElement("div");div.className="item";
const im=a.impact?'<div class="muted">'+a.impact+'</div>':'';
div.innerHTML='<span class="tag '+a.priority+'">'+a.priority+'</span>'+a.title+im;b.appendChild(div);}}
btn.addEventListener("click",start);input.addEventListener("keydown",e=>{if(e.key==="Enter")start();});
async function checkStatus(){try{const r=await fetch('/api/status');const s=await r.json();
const c=document.getElementById('conn');
if(s.connected){c.innerHTML='\u2705 Conectado ao Mercado Livre';}
else if(!s.configured){c.innerHTML='\u26a0\ufe0f Falta configurar as credenciais do Mercado Livre no servidor (ML_CLIENT_ID, ML_CLIENT_SECRET, ML_REDIRECT_URI).';}
else{c.innerHTML='<a href="/oauth/login" style="color:#3483fa;font-weight:600">&#x1F517; Conectar com Mercado Livre</a> (necessário antes de analisar)';}
}catch(e){}}
checkStatus();
</script></body></html>"""
