# Resilient Payment Client

Client HTTP resiliente para integração com uma API fictícia de pagamentos, desenvolvido como teste técnico para a **Voa Health**. A aplicação expõe um endpoint REST que delega chamadas à API externa via `PaymentClient`, uma classe com retry automático, backoff exponencial com jitter, timeout configurável e logging estruturado em JSON.

---

## Sumário

1. [Decisões técnicas](#decisões-técnicas)
2. [Arquitetura](#arquitetura)
3. [Setup e execução](#setup-e-execução)
4. [Como testar](#como-testar)
5. [Variáveis de ambiente](#variáveis-de-ambiente)
6. [Uso de IA](#uso-de-ia)

---

## Decisões técnicas

Esta seção detalha cada escolha de tecnologia e o raciocínio por trás dela.

### FastAPI (ao invés de Django ou Flask)

FastAPI foi escolhido por três razões principais:

- **Performance assíncrona nativa.** FastAPI é construído sobre ASGI (Starlette), o que permite tratar centenas de requisições concorrentes sem bloquear o event loop, essencial quando o endpoint precisa aguardar a API externa (que pode levar segundos).
- **Validação integrada via Pydantic.** O body do request é validado automaticamente antes de chegar ao handler, eliminando código de validação manual e retornando erros 422 descritivos sem esforço extra.
- **Documentação OpenAPI automática.** Ao acessar `/docs`, o avaliador vê todos os endpoints, schemas e exemplos sem configuração adicional.
- (e porque eu já tenho familiaridade com esse framework)

Django seria excessivo para uma API pequena e orientada a I/O (seu ORM, templates e admin são overhead desnecessário). Flask não tem suporte nativo a async, exigindo extensões adicionais.

### httpx (ao invés de requests)

`requests` é síncrono. Em um endpoint FastAPI assíncrono, uma chamada síncrona bloquearia o event loop durante o request inteiro, anulando todo o benefício do ASGI.

`httpx` expõe uma API quase idêntica à do `requests`, mas com suporte nativo a `async/await` via `httpx.AsyncClient`. Além disso, o `AsyncClient` mantém um pool de conexões TCP/TLS reutilizáveis entre requests, reduzindo latência e overhead de handshake.

### Async/await em todo o I/O

Todo I/O da aplicação (requests HTTP, o `asyncio.sleep` do backoff, os endpoints FastAPI) é assíncrono. Isso permite que um único processo sirva múltiplas requisições concorrentes enquanto aguarda respostas da API externa, sem precisar de threads ou processos extras.

O `asyncio.sleep` no backoff entre retries é especialmente importante: garante que o servidor continue respondendo a outras requisições durante o intervalo de espera entre tentativas.

### structlog (ao invés de logging padrão)

O módulo `logging` da stdlib produz strings não estruturadas, difíceis de parsear em sistemas de observabilidade (Datadog, Loki, Grafana). O `structlog` produz JSON com campos nomeados:

```json
{
  "event": "http_request_complete",
  "method": "POST",
  "url": "http://localhost:8001/charges",
  "status_code": 201,
  "duration_ms": 42.7,
  "attempt": 1,
  "level": "info",
  "timestamp": "2026-03-15T14:30:00.123Z"
}
```

Cada campo pode ser filtrado, agregado ou correlacionado em dashboards. O mesmo código funciona em modo `console` (logs coloridos para desenvolvimento) ou `json` (produção), controlado pela variável `LOG_FORMAT`.

### pydantic-settings (ao invés de variáveis hardcoded)

`pydantic-settings` lê variáveis de ambiente e arquivos `.env`, valida os tipos automaticamente e fornece defaults sensatos. Sem ele, seria necessário `os.getenv("...")` espalhado pelo código, com risco de variáveis mal tipadas ou ausentes causando erros em runtime. O `@lru_cache` em `get_settings()` garante que o `.env` seja lido apenas uma vez por processo.

### uv (ao invés de pip)

`uv` é um gerenciador de pacotes escrito em Rust, tipicamente 10-100x mais rápido que `pip`. Além da velocidade, gera e respeita um `uv.lock` que fixa versões exatas de todas as dependências transitivas, garantindo builds reproduzíveis em qualquer ambiente (desenvolvimento, CI, Docker etc.).

### Hierarquia de exceções tipadas

Erros da API externa são mapeados em exceções específicas (`PaymentTimeoutError`, `PaymentUnavailableError`, `PaymentClientError`, `PaymentNotFoundError`, `PaymentConnectionError`), cada uma carregando `status_code`, `retries_attempted` e `response_body`. Isso permite que os exception handlers globais do FastAPI mapeiem cada tipo de falha para o status HTTP correto (504, 502, 400, 404) de forma centralizada e sem duplicação de código.

### Decimal para valores monetários

`float` tem imprecisão de ponto flutuante: `0.1 + 0.2 == 0.30000000000000004`. Para valores monetários, qualquer arredondamento indevido é inaceitável. `Decimal` oferece precisão arbitrária e aritmética exata.

---

## Arquitetura

### Estrutura de diretórios

```
resilient-payment-client/
├── app/
│   ├── main.py              # FastAPI app, lifespan, exception handlers globais
│   ├── config.py            # Settings via pydantic-settings
│   ├── client/
│   │   ├── payment_client.py  # Núcleo: HTTP + retry + logging
│   │   ├── exceptions.py      # Hierarquia de exceções tipadas
│   │   └── schemas.py         # Modelos Pydantic (request/response)
│   └── api/
│       └── charges.py         # Router REST: POST /api/charges/
├── mock_server/
│   └── server.py            # Simulador da API externa (porta 8001)
├── tests/
│   ├── conftest.py          # Fixtures compartilhadas
│   ├── test_client.py       # Testes de operações CRUD
│   ├── test_retry.py        # Testes de retry e backoff
│   ├── test_timeout.py      # Testes de timeout
│   └── test_endpoint.py     # Testes de integração do endpoint REST
├── Dockerfile               # Imagem da app principal
├── Dockerfile.mock          # Imagem do mock server
├── docker-compose.yml       # Orquestração dos dois serviços
├── pyproject.toml           # Metadados e dependências
└── .env.example             # Variáveis de ambiente documentadas
```

### Fluxo de dados

```
Consumidor (curl / cliente HTTP)
         │
         │  POST /api/charges/
         │  { "amount": 100.00, "currency": "BRL", "description": "Pedido" }
         ▼
┌─────────────────────────────────┐
│         FastAPI (porta 8000)    │
│                                 │
│  ┌──────────────────────────┐   │
│  │  Pydantic (validação)    │   │  ← 422 se body inválido
│  └──────────┬───────────────┘   │
│             │                   │
│  ┌──────────▼───────────────┐   │
│  │  POST /api/charges/      │   │
│  │  (charges.py)            │   │
│  └──────────┬───────────────┘   │
│             │ Depends()         │
│  ┌──────────▼───────────────┐   │
│  │  PaymentClient           │   │
│  │  ┌─────────────────────┐ │   │
│  │  │  _request()         │ │   │
│  │  │  ┌───────────────┐  │ │   │
│  │  │  │ Tentativa 1   │  │ │   │
│  │  │  │ (httpx)       │  │ │   │
│  │  │  └──────┬────────┘  │ │   │
│  │  │         │ 5xx/timeout│ │   │
│  │  │  ┌──────▼────────┐  │ │   │
│  │  │  │ backoff + sleep│  │ │   │
│  │  │  └──────┬────────┘  │ │   │
│  │  │  ┌──────▼────────┐  │ │   │
│  │  │  │ Tentativa 2   │  │ │   │
│  │  │  └──────┬────────┘  │ │   │
│  │  │         │ ...       │ │   │
│  │  │  ┌──────▼────────┐  │ │   │
│  │  │  │ Tentativa N   │  │ │   │
│  │  │  └───────────────┘  │ │   │
│  │  └─────────────────────┘ │   │
│  └──────────┬───────────────┘   │
│             │                   │
│  ┌──────────▼───────────────┐   │
│  │  Exception Handlers      │   │
│  │  Timeout → 504           │   │
│  │  5xx     → 502           │   │
│  │  4xx     → 400           │   │
│  │  404     → 404           │   │
│  └──────────────────────────┘   │
└─────────────────────────────────┘
         │
         │  POST /charges
         ▼
┌─────────────────────────────────┐
│    Mock Payment API (porta 8001)│
│    (ou API de pagamentos real)  │
│                                 │
│  POST   /charges          → 201 │
│  GET    /charges/{id}     → 200 │
│  GET    /charges          → 200 │
│  POST   /charges/{id}/refund    │
│                                 │
│  Comportamentos especiais:      │
│  description "timeout"   → 60s  │
│  description "error_500" → 500  │
│  description "flaky"     → 500  │
│                          → 500  │
│                          → 201  │
└─────────────────────────────────┘
```

### Lógica de retry

```
Para cada tentativa (attempt 1 até max_retries):
  │
  ├─ 2xx → retorna response ✓
  │
  ├─ 404 → PaymentNotFoundError (sem retry)
  │
  ├─ 4xx (exceto 429) → PaymentClientError (sem retry)
  │
  ├─ 5xx ou 429 → loga warning, aguarda backoff, tenta novamente
  │
  ├─ Timeout → loga warning, aguarda backoff, tenta novamente
  │
  └─ ConnectError → loga warning, aguarda backoff, tenta novamente

Backoff: delay = base × 2^attempt + jitter (jitter ∈ [0, 0.5s])
  attempt=0 → ~1.0s  |  attempt=1 → ~2.0s  |  attempt=2 → ~4.0s

Após esgotar tentativas:
  Último erro foi Timeout     → PaymentTimeoutError     → HTTP 504
  Último erro foi ConnectError → PaymentConnectionError → HTTP 502
  Último erro foi 5xx/429     → PaymentUnavailableError → HTTP 502
```

---

## Setup e execução

### Opção 1: Docker (recomendado)

Sobe a aplicação principal (porta 8000) e o mock server (porta 8001) em containers isolados. O app só inicia após o mock-api passar no healthcheck.

**Pré-requisito:** Docker Desktop instalado e em execução.

```bash
# Constrói as imagens e inicia ambos os serviços
docker-compose up --build

# Para encerrar
docker-compose down
```

Após subir, acesse:
- App principal: http://localhost:8000/docs
- Mock server: http://localhost:8001/docs

---

### Opção 2: Sem Docker, com uv (desenvolvimento)

**Pré-requisito:** [`uv`](https://docs.astral.sh/uv/getting-started/installation/) instalado.

```bash
# 1. Instala todas as dependências (cria .venv automaticamente)
uv sync

# 2. Copia o arquivo de variáveis de ambiente
copy .env.example .env

# 3. Em um terminal, inicia o mock server (porta 8001)
uv run uvicorn mock_server.server:app --port 8001 --reload

# 4. Em outro terminal, inicia a aplicação principal (porta 8000)
uv run uvicorn app.main:app --port 8000 --reload
```

---

### Opção 3: Sem Docker, com pip

**Pré-requisito:** Python 3.12+ instalado.

```bash
# 1. Cria e ativa o ambiente virtual
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Linux/macOS
source .venv/bin/activate

# 2. Instala as dependências
pip install -r requirements.txt

# 3. Copia o arquivo de variáveis de ambiente
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/macOS

# 4. Em um terminal, inicia o mock server (porta 8001)
uvicorn mock_server.server:app --port 8001 --reload

# 5. Em outro terminal, inicia a aplicação principal (porta 8000)
uvicorn app.main:app --port 8000 --reload
```

---

## Como testar

### Testes automatizados

Os testes usam `pytest` + `respx` para interceptar as chamadas HTTP do `httpx` sem depender de nenhum servidor externo rodando.

```bash
# Roda todos os testes com saída detalhada
uv run pytest tests/ -v

# Roda um arquivo específico
uv run pytest tests/test_retry.py -v

# Roda um teste específico
uv run pytest tests/test_retry.py::test_retry_on_500_then_success -v
```

Suítes de testes:

| Arquivo | O que testa |
|---|---|
| `test_client.py` | Operações CRUD: criar, consultar, listar, reembolsar |
| `test_retry.py` | Retry em 5xx, esgotamento de tentativas, sem retry em 4xx |
| `test_timeout.py` | Timeout levanta `PaymentTimeoutError`, retry com timeout seguido de sucesso |
| `test_endpoint.py` | Endpoint REST integrado: 201, 422, 504, 502, 404 |

---

### Testes manuais com curl

Com os dois servidores rodando (Docker ou uv), execute:

```bash
# Criação de cobrança -> sucesso (retorna 201)
curl -X POST http://localhost:8000/api/charges/ \
  -H "Content-Type: application/json" \
  -d "{\"amount\": 100.00, \"currency\": \"BRL\", \"description\": \"Pedido #42\"}"

# Body inválido -> validação Pydantic (retorna 422)
curl -X POST http://localhost:8000/api/charges/ \
  -H "Content-Type: application/json" \
  -d "{\"amount\": -1, \"currency\": \"BRL\", \"description\": \"Teste\"}"

# Simulação de timeout -> após retries, retorna 504
# (aguarde: o mock espera 60s, o client esgota os retries e falha)
curl -X POST http://localhost:8000/api/charges/ \
  -H "Content-Type: application/json" \
  -d "{\"amount\": 100.00, \"currency\": \"BRL\", \"description\": \"timeout\"}"

# Simulação de erro 500 -> após retries, retorna 502
curl -X POST http://localhost:8000/api/charges/ \
  -H "Content-Type: application/json" \
  -d "{\"amount\": 100.00, \"currency\": \"BRL\", \"description\": \"error_500\"}"

# Simulação de servidor instável (flaky) -> retorna 201 na 3ª tentativa
# Observe os logs do servidor: verá "http_request_retriable_error" nas 2 primeiras
curl -X POST http://localhost:8000/api/charges/ \
  -H "Content-Type: application/json" \
  -d "{\"amount\": 100.00, \"currency\": \"BRL\", \"description\": \"flaky\"}"
```

---

### Documentação interativa (Swagger UI)

Com os servidores rodando, acesse http://localhost:8000/docs para explorar e testar os endpoints diretamente pelo navegador.

---

## Variáveis de ambiente

Todas as variáveis são lidas do arquivo `.env` na raiz do projeto. Valores default permitem rodar sem `.env` em desenvolvimento.

| Variável | Descrição | Default |
|---|---|---|
| `PAYMENT_API_BASE_URL` | URL base da API de pagamentos (mock ou real) | `http://localhost:8001` |
| `PAYMENT_API_CONNECT_TIMEOUT` | Timeout de conexão TCP/TLS em segundos | `5.0` |
| `PAYMENT_API_READ_TIMEOUT` | Timeout de leitura da resposta em segundos | `30.0` |
| `PAYMENT_API_MAX_RETRIES` | Número máximo de tentativas por request | `3` |
| `PAYMENT_API_BACKOFF_BASE` | Base em segundos para o backoff exponencial | `1.0` |
| `LOG_LEVEL` | Nível de log: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `LOG_FORMAT` | Formato dos logs: `json` (produção) ou `console` (desenvolvimento) | `json` |

**Exemplo de `.env` para desenvolvimento local:**

```env
PAYMENT_API_BASE_URL=http://localhost:8001
PAYMENT_API_CONNECT_TIMEOUT=5.0
PAYMENT_API_READ_TIMEOUT=30.0
PAYMENT_API_MAX_RETRIES=3
PAYMENT_API_BACKOFF_BASE=1.0
LOG_LEVEL=INFO
LOG_FORMAT=console
```

> Dica: use `LOG_FORMAT=console` localmente para logs coloridos e legíveis por humanos. Em produção (Docker, CI), use `LOG_FORMAT=json` para facilitar a ingestão em ferramentas de observabilidade.

---

## Uso de IA

### Ferramenta utilizada

**Claude** (Anthropic), modelos Sonnet 4.6 e Opus 4.6, via [Cursor IDE](https://www.cursor.com/).