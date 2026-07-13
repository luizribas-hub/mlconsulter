# ML Audit — Backend (MVP)

Backend do consultor de IA para otimização de anúncios do Mercado Livre.

## O que já existe (passo 1 + Módulo 4)

- Integração com a API pública do Mercado Livre (item, descrição, vendedor, categoria, busca) com cache em Redis
- Normalização item bruto → `ItemSnapshot` (os módulos não dependem do JSON cru do ML)
- Pipeline assíncrono (Celery + Redis): a API só enfileira, o worker processa
- **Módulo 4 — Benchmark**: descobre concorrentes, identifica papéis (líder de vendas, líder orgânico, mais barato) e calcula agregados do nicho (preço médio/mediano/min/max)
- Camada **Consultor IA**: transforma os achados em plano de ação priorizado; usa Claude quando `ANTHROPIC_API_KEY` está setada, com fallback determinístico sem IA
- Persistência: `analyses`, `analysis_scores`, `action_items`

## Estrutura

```
app/
  core/          config + cache Redis
  integrations/  cliente do Mercado Livre + normalizador
  modules/       módulos de análise (base + benchmark)
  workers/       orquestrador, consultor IA, fila Celery
  models/        modelos de banco (SQLAlchemy)
  schemas/       contratos de API (Pydantic)
  api/           rotas HTTP
  main.py        entrypoint FastAPI
```

## Como rodar (2 modos)

### Modo simples — 1 serviço só (recomendado para colocar online)
Não precisa de Redis nem Postgres. Usa SQLite e roda a análise na própria API.

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Abra `http://localhost:8000` — tem uma tela pronta pra colar o MLB.

### Colocar online (link público, sem instalar nada no seu desktop)

Você precisa de uma conta grátis num serviço de hospedagem. O código já vem
pronto (`render.yaml` e `Procfile`). Caminho mais simples, via **Render**:

1. Crie uma conta em render.com (grátis).
2. Suba este projeto para um repositório no GitHub.
3. No Render: **New → Blueprint**, aponte para o repositório. Ele lê o
   `render.yaml` e cria o serviço sozinho.
4. (Opcional) Em Environment, cole sua `ANTHROPIC_API_KEY` para ativar a
   narrativa completa do consultor.
5. Pronto: o Render te dá um link `https://ml-audit.onrender.com`.

Alternativas equivalentes: **Railway** (usa o `Procfile`) ou **Replit**
(importa o zip e roda no navegador, sem GitHub).

> Observação: no plano grátis o SQLite reinicia a cada novo deploy — ok para
> validar. Para produção real, troque `DATABASE_URL` por um Postgres e ligue
> `USE_CELERY=true` com Redis (a arquitetura já suporta, é só configurar).

### Modo escalável — API + worker separados
```bash
uvicorn app.main:app --reload            # terminal 1
celery -A app.workers.queue.celery_app worker --loglevel=info   # terminal 2
```
Requer `USE_CELERY=true`, Redis e (idealmente) Postgres no `.env`.

## Fluxo da API

```bash
# cria a análise (retorna analysis_id)
curl -X POST localhost:8000/api/analysis \
  -H "Content-Type: application/json" \
  -d '{"input": "MLB123456789"}'

# consulta o resultado (faça polling até status = "done")
curl localhost:8000/api/analysis/<analysis_id>
```

Aceita tanto o código `MLB...` quanto o link completo do anúncio.

## Próximos módulos (ordem por impacto)

1. Diagnóstico Geral (casca que os módulos preenchem) — parcial: nota geral já é ponderada no orquestrador
2. Preço (Módulo 7) e Reputação (Módulo 9) — leitura simples, validam o pipeline fim a fim
3. Título (Módulo 6) — primeira sugestão via IA
4. Posicionamento na busca (Módulo 2)
5. Fotos (Módulo 5) — IA multimodal
6. Checklist final (Módulos 12/14) — já esboçado em `workers/consultant.py`

Quando um módulo novo entra, ele é adicionado em `PIPELINE` e em `SCORE_WEIGHTS`
no `orchestrator.py` — nada mais precisa mudar.
```
