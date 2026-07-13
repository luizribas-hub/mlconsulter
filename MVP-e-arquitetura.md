# MVP Estratégico + Arquitetura Técnica
## Sistema Inteligente de Auditoria e Otimização de Anúncios do Mercado Livre

---

## 1. Princípio de priorização do MVP

Critério usado para escolher o que entra na v1: **(impacto percebido em vendas) × (viabilidade técnica com dados públicos)**, descartando por enquanto tudo que depende de histórico próprio (monitoramento contínuo) ou de modelos preditivos treinados (que precisam de dados acumulados que ainda não existem).

A regra de ouro do produto — "não é dashboard, é consultor" — precisa estar presente **desde o MVP**, mesmo que cubra menos módulos. Por isso o MVP sempre termina em um plano de ação gerado por IA, nunca só em números.

---

## 2. Escopo do MVP (v1)

### Incluído

| # | Módulo | Por quê entra no MVP |
|---|--------|----------------------|
| 1 | Diagnóstico Geral (nota 0–100) | É a âncora do produto; todo o resto alimenta essa nota |
| 4 | Benchmark automático (achar concorrentes) | Pré-requisito técnico de quase tudo mais — sem isso não há "comparado a quem" |
| 6 | Análise de Título (SEO) | Alto impacto, dado 100% acessível via API pública, fácil de gerar "sugestão de título melhor" com LLM |
| 5 | Análise de Fotos (versão enxuta) | Altíssimo impacto em conversão; viável no MVP usando um modelo multimodal para avaliar as fotos já públicas, sem precisar de CV treinado do zero |
| 7 | Preço | Dado público, comparação trivial, impacto direto e fácil de explicar |
| 9 | Reputação | Disponível via API pública do vendedor, sem complexidade extra |
| 2 | Posicionamento na busca (versão simples) | Dá para calcular consultando a busca pública para as palavras-chave do título e localizando a posição do MLB |
| 12/14 | Checklist + "plano de ação do consultor" | Este é o entregável real do produto — converte os módulos acima em ações priorizadas com impacto/dificuldade/tempo estimado |

### Fora do MVP (v1.5 / v2)

- Módulo 15/16 (Score de SEO e Score Comercial como índices separados) — nascem como subprodutos dos módulos acima; formalizar como "score" próprio fica pra v1.5
- Módulo 3 (Relevância do algoritmo como índice isolado) — mesma lógica, é uma composição do que já foi calculado
- Módulo 10 (Descrição) — v1.5, reaproveita a mesma pipeline de texto do título
- Módulo 8 (Frete) — v1.5, depende de campos de shipping que exigem chamadas extras
- Módulo 13 (Inteligência competitiva agregada) — v1.5, é o Módulo 4 aprofundado
- Módulo 18 (PDF executivo) — v1.5, é "empacotamento", não geração de insight novo
- Módulo 17 (Simulador de melhorias) — v2, depende de ter os scores já validados em produção
- Módulo 19 (Monitoramento contínuo) — v2, exige histórico acumulado + jobs agendados + notificações
- Módulo 20 (Inteligência preditiva) — v2/v3, exige base histórica real para treinar/calibrar; sem dados acumulados seria só "achismo" da IA

### Critério de saída do MVP
O MVP está pronto quando um usuário consegue: colar um MLB → receber nota geral, comparação com concorrentes, diagnóstico de título/fotos/preço/reputação/posição na busca → e um plano de ação priorizado e explicado em linguagem de consultor, sem tocar em nada além de "colar o código e esperar".

---

## 3. Arquitetura técnica

### 3.1 Visão geral

```
[ Usuário ]
     │  cola MLB ou link
     ▼
[ Frontend (Next.js) ] ──────────────┐
     │  POST /analysis                │ GET /analysis/:id/status (polling ou WS)
     ▼                                │
[ API Gateway / BFF (Node ou FastAPI) ]
     │
     ▼
[ Orquestrador de Análise ] ── enfileira job ──▶ [ Fila (Redis + BullMQ/Celery) ]
     │                                                │
     │                                                ▼
     │                                    [ Workers assíncronos ]
     │                                      ├─ Worker: coleta ML API (item, seller, busca)
     │                                      ├─ Worker: coleta concorrentes (benchmark)
     │                                      ├─ Worker: análise de fotos (IA multimodal)
     │                                      ├─ Worker: análise de título/texto (IA)
     │                                      └─ Worker: scoring engine (regras determinísticas)
     ▼
[ Camada de síntese / "Consultor IA" ] ── consolida todos os scores + achados
     │  gera narrativa + plano de ação priorizado
     ▼
[ Postgres ] (persistência: análises, scores, histórico, usuários)
[ Redis ] (cache de chamadas à API do ML, rate-limit, filas)
[ Object Storage (S3-compatible) ] (fotos baixadas, thumbnails, relatórios)
```

### 3.2 Por que assíncrono desde o MVP
A análise completa (buscar item, achar concorrentes, rodar IA em várias fotos, gerar texto) não cabe numa requisição HTTP síncrona sem o usuário sentir travamento. Por isso, mesmo no MVP:
- `POST /analysis` cria o job e retorna um `analysis_id` imediatamente
- Frontend faz polling (ou WebSocket/SSE) em `/analysis/:id` até status = `done`
- Isso também resolve escalabilidade: workers podem crescer horizontalmente sem tocar no BFF

### 3.3 Stack sugerida

| Camada | Escolha | Motivo |
|---|---|---|
| Frontend | Next.js (React) + Tailwind | SSR/SEO para páginas de marketing, boa DX, deploy simples (Vercel) |
| BFF / API | Node.js (NestJS) **ou** Python (FastAPI) | FastAPI se a maior parte da lógica de scoring/IA ficar em Python (mais natural para pipelines de IA); NestJS se o time for mais forte em TS. Recomendo **FastAPI** pela proximidade com as libs de IA/imagem |
| Fila / jobs | Redis + Celery (Python) ou BullMQ (Node) | Processamento assíncrono dos workers |
| Banco de dados | PostgreSQL | Dados relacionais (usuários, análises, scores, histórico), suporta JSONB para os payloads flexíveis de cada módulo |
| Cache | Redis | Cache de respostas da API do Mercado Livre (itens/categorias mudam pouco em minutos), controle de rate limit |
| Storage de arquivos | S3 / Cloudflare R2 | Fotos baixadas para análise, relatórios PDF (v1.5+) |
| IA multimodal | API da Anthropic (Claude) | Análise de fotos, título, descrição e geração da narrativa de consultor — um único provedor simplifica manutenção |
| Integração Mercado Livre | API pública REST (`api.mercadolibre.com`) | Itens, vendedores, categorias e busca — não exige OAuth para dados públicos de itens/busca |
| Autenticação de usuário | Auth gerenciada (ex.: Clerk/Auth0) ou e-mail mágico simples | MVP pode funcionar sem login para uma análise avulsa; login vira necessário a partir do histórico/monitoramento (v2) |
| Observabilidade | Logs estruturados + Sentry | Essencial desde o MVP porque há muita dependência de API de terceiros (ML) que pode falhar/mudar |

### 3.4 Modelo de dados (núcleo, simplificado)

```
users            (id, email, plano, created_at)
analyses         (id, user_id nullable, mlb_id, status, created_at, finished_at)
analysis_scores  (analysis_id, modulo, nota, detalhes_jsonb)
competitors      (analysis_id, mlb_id_concorrente, papel [lider_vendas|lider_patrocinado|...], dados_jsonb)
action_items     (analysis_id, prioridade [alta|media|baixa], titulo, impacto_estimado, dificuldade, tempo_estimado, ordem)
```
`analyses` e `action_items` são o coração do produto: sem `action_items` bem estruturado, a IA vira só um textão — e o objetivo é o plano de ação ser navegável/checkável na interface.

### 3.5 Design da "camada consultor"
Esta é a parte que diferencia o produto de um dashboard: depois que todos os workers terminam e os scores determinísticos existem, um único prompt de síntese recebe **todos os dados estruturados** (não texto solto) e produz:
1. Nota geral + classificação
2. 3 a 5 pontos fortes / fracos com impacto estimado
3. Plano de ação ordenado (igual ao Módulo 12/14), cada item com impacto, dificuldade e tempo
4. Resposta explícita à pergunta "o que eu faria se este anúncio fosse meu?"

Importante: os **scores** (nota geral, nota de foto, nota de título etc.) devem ser calculados por regras determinísticas ponderadas primeiro (não pedir pro modelo "inventar" um número), e a IA generativa entra só na camada de explicação e priorização. Isso torna o produto mais consistente e auditável — e mais barato, porque a IA não recalcula tudo do zero a cada request.

### 3.6 Escalabilidade — decisões que evitam retrabalho depois
- Workers desacoplados por módulo desde o dia 1 (mesmo que rodem no mesmo processo no MVP) — facilita extrair para serviços separados quando o volume crescer
- Cache agressivo de chamadas à API do Mercado Livre por `mlb_id` com TTL curto (ex. 15–30 min) — evita rate limit e deixa reprocessamento barato
- Scores gravados em `JSONB` versionado — permite mudar a fórmula de peso de um score sem quebrar análises antigas (grava-se também `versao_scoring`)
- Fila desde o MVP (não endpoint síncrono) — não faz sentido reescrever isso depois só porque "no começo era simples"

---

## 4. Ordem de desenvolvimento sugerida (por impacto)

1. **Infra base**: BFF + fila + worker skeleton + Postgres + integração autenticada com API do Mercado Livre (item + busca + vendedor)
2. **Módulo 4 (Benchmark)** — precisa vir cedo porque quase tudo depois compara com concorrentes
3. **Módulo 1 (Diagnóstico Geral)** — como "casca vazia" que vai sendo preenchida pelos módulos seguintes
4. **Módulo 7 (Preço)** e **Módulo 9 (Reputação)** — mais simples, só leitura de API, ótimos para validar o pipeline fim a fim rápido
5. **Módulo 6 (Título)** — primeira entrada de IA generativa (sugestão de títulos)
6. **Módulo 2 (Posicionamento na busca)** — precisa do módulo de título/keywords já pronto
7. **Módulo 5 (Fotos)** — mais caro/lento (chamadas multimodais), por isso entra depois de validar o resto
8. **Módulo 12/14 (Checklist + Consultor IA)** — fecha o MVP, consolidando tudo

Quer que eu comece já pelo passo 1 (esqueleto de infraestrutura + integração com a API do Mercado Livre) ou prefere primeiro validar o modelo de dados e os contratos de API entre frontend/backend antes de codar?
