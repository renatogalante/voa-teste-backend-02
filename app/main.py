"""
Aplicação principal FastAPI — Client HTTP Resiliente para API de Pagamentos.

Este módulo é o ponto de entrada da aplicação. Suas responsabilidades são:

  1. Criar e configurar a instância FastAPI com título, descrição e versão.
  2. Configurar o structlog no startup (via lifespan) para que todos os módulos
     produzam logs no formato correto desde a primeira requisição.
  3. Registrar os routers dos endpoints (charges, e futuros módulos).
  4. Registrar exception handlers globais que mapeiam as exceções semânticas do
     PaymentClient para os status HTTP corretos na resposta ao consumidor.

Decisão de design — exception handlers globais vs try/except no endpoint:
    Registrar os handlers globalmente em app (em vez de capturar em cada endpoint)
    evita duplicação de código e garante consistência no mapeamento de erros em
    todos os endpoints presentes e futuros. O endpoint fica limpo — apenas lógica
    de negócio — e o mapeamento de erros fica centralizado aqui.

Ordem de registro dos exception handlers:
    PaymentNotFoundError DEVE ser registrado ANTES de PaymentClientError porque
    NotFoundError herda de ClientError. O FastAPI verifica os handlers na ordem
    de registro; se ClientError fosse primeiro, capturaria os NotFoundError antes
    que o handler mais específico tivesse chance de agir.
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.charges import router as charges_router
from app.client.exceptions import (
    PaymentClientError,
    PaymentConnectionError,
    PaymentNotFoundError,
    PaymentTimeoutError,
    PaymentUnavailableError,
)
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gerencia o ciclo de vida da aplicação FastAPI.

    O bloco antes do yield é executado no startup (antes de aceitar requisições).
    O bloco após o yield é executado no shutdown (após parar de aceitar requisições).

    Usamos o lifespan para configurar o structlog no startup garantindo que os logs
    estejam no formato correto antes que qualquer request seja processado.
    O lifespan é a forma recomendada pelo FastAPI (>=0.95) para lógica de startup
    e shutdown, substituindo os eventos @app.on_event (deprecated).

    Args:
        app: Instância do FastAPI — disponível para acessar state ou configurações
             se necessário em versões futuras.

    Yields:
        Nenhum valor — o yield apenas separa startup de shutdown.
    """
    # ── Startup: configura structlog com o formato definido nas settings ───
    settings = get_settings()

    # Escolhe o renderizador baseado na configuração LOG_FORMAT:
    #   "console" → saída colorida legível por humanos (desenvolvimento)
    #   qualquer outro valor → JSON estruturado (padrão, produção)
    if settings.log_format == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            # Adiciona o nível de log (info/warning/error) ao dict de contexto
            structlog.stdlib.add_log_level,
            # Adiciona timestamp ISO 8601 a cada log entry
            structlog.processors.TimeStamper(fmt="iso"),
            # Renderiza o log final no formato escolhido
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Cache do logger melhora performance em endpoints com muitas chamadas
        cache_logger_on_first_use=True,
    )

    log = structlog.get_logger().bind(module="main")
    log.info(
        "application_startup",
        payment_api_base_url=settings.payment_api_base_url,
        max_retries=settings.payment_api_max_retries,
        log_format=settings.log_format,
    )

    # Cede o controle ao FastAPI para aceitar requisições
    yield

    # ── Shutdown: loga encerramento ────────────────────────────────────────
    log.info("application_shutdown")


# ─────────────────────────────────────────────────────────────────────────────
# Instância da aplicação FastAPI
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Resilient Payment Client",
    description=(
        "Client HTTP resiliente para integração com API de pagamentos. "
        "Implementa retry automático com backoff exponencial, timeout configurável "
        "e logging estruturado em JSON."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ─────────────────────────────────────────────────────────────────────────────
# Registro de routers
# ─────────────────────────────────────────────────────────────────────────────

# Prefixo /api/charges garante que o endpoint fique em POST /api/charges/
app.include_router(charges_router, prefix="/api/charges", tags=["charges"])

# ─────────────────────────────────────────────────────────────────────────────
# Exception handlers globais
#
# ORDEM DE REGISTRO É CRÍTICA:
#   PaymentNotFoundError herda de PaymentClientError.
#   Se ClientError fosse registrado primeiro, o FastAPI o usaria para
#   capturar NotFoundError também, retornando 400 em vez de 404.
#   Registrar o mais específico (NotFoundError) antes do mais genérico
#   (ClientError) garante o mapeamento correto.
# ─────────────────────────────────────────────────────────────────────────────


@app.exception_handler(PaymentNotFoundError)
async def handle_not_found(request, exc: PaymentNotFoundError) -> JSONResponse:
    """
    Mapeia PaymentNotFoundError para HTTP 404 Not Found.

    Levantada pelo PaymentClient quando a API de pagamentos retorna 404.
    Indica que o recurso solicitado (ex: cobrança com ID inexistente) não existe.
    DEVE ser registrado antes de PaymentClientError (veja comentário acima).
    """
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc)},
    )


@app.exception_handler(PaymentClientError)
async def handle_client_error(request, exc: PaymentClientError) -> JSONResponse:
    """
    Mapeia PaymentClientError para HTTP 400 Bad Request.

    Levantada pelo PaymentClient quando a API de pagamentos retorna 4xx (exceto 404).
    Indica erro no request enviado — payload inválido, token expirado, etc.
    """
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc)},
    )


@app.exception_handler(PaymentTimeoutError)
async def handle_timeout(request, exc: PaymentTimeoutError) -> JSONResponse:
    """
    Mapeia PaymentTimeoutError para HTTP 504 Gateway Timeout.

    Levantada quando a API de pagamentos não respondeu dentro do tempo configurado
    em todas as tentativas de retry. Indica que o gateway (nossa app) não recebeu
    resposta do upstream (API de pagamentos) em tempo hábil.
    """
    return JSONResponse(
        status_code=504,
        content={"detail": str(exc)},
    )


@app.exception_handler(PaymentUnavailableError)
async def handle_unavailable(request, exc: PaymentUnavailableError) -> JSONResponse:
    """
    Mapeia PaymentUnavailableError para HTTP 502 Bad Gateway.

    Levantada quando a API de pagamentos retornou 5xx em todas as tentativas
    de retry. Indica que o serviço upstream está fora ou com falha interna.
    """
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc)},
    )


@app.exception_handler(PaymentConnectionError)
async def handle_connection_error(request, exc: PaymentConnectionError) -> JSONResponse:
    """
    Mapeia PaymentConnectionError para HTTP 502 Bad Gateway.

    Levantada quando não foi possível estabelecer conexão com a API de pagamentos
    (DNS não resolvido, conexão recusada, reset de rede) após todas as tentativas.
    """
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc)},
    )
