"""
Router REST para operações de cobranças (charges).

Este módulo define o APIRouter do FastAPI que expõe o endpoint POST /api/charges/,
ponto de entrada público para criação de cobranças via PaymentClient. O router é
registrado em app/main.py com o prefixo /api/charges.

A dependency get_payment_client é definida aqui (e não em main.py) para evitar
import circular: main.py inclui este router, então charges.py não pode importar
de main.py. Definir a dependency próxima do código que a utiliza também segue o
princípio de coesão — a lógica de obter o client pertence ao módulo que o usa.

Decisões de design:
    - O PaymentClient é injetado via Depends() para facilitar substituição em testes
      (basta sobrescrever a dependência com um mock) sem alterar a lógica do endpoint.
    - O status_code=201 segue a semântica REST: POST que cria recurso retorna Created.
    - As exceções do client (PaymentClientError, PaymentTimeoutError, etc.) NÃO são
      capturadas aqui — elas sobem para os exception handlers globais registrados em
      app/main.py, que as mapeiam para os status HTTP corretos (400, 504, 502, etc.).
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.client.payment_client import PaymentClient
from app.client.schemas import ChargeResponse, CreateChargeRequest
from app.config import get_settings

router = APIRouter()


async def get_payment_client() -> AsyncGenerator[PaymentClient, None]:
    """
    Dependency FastAPI que fornece uma instância de PaymentClient por requisição.

    Usa o async context manager do PaymentClient para garantir que o
    httpx.AsyncClient seja aberto antes do endpoint ser chamado e fechado
    (com drenagem do pool de conexões) após o retorno — mesmo em caso de exceção.

    O padrão async generator com yield é o idioma FastAPI para dependencies
    com cleanup: o código antes do yield é o "setup" e o código após (dentro
    do bloco finally implícito) é o "teardown".

    Yields:
        PaymentClient já inicializado (dentro do context manager), pronto para
        fazer chamadas HTTP ao mock server ou à API de pagamentos real.
    """
    settings = get_settings()
    async with PaymentClient(settings) as client:
        yield client


@router.post(
    "/",
    response_model=ChargeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Criar cobrança",
    description=(
        "Cria uma nova cobrança na API de pagamentos. "
        "Aplica retry automático com backoff exponencial em falhas transitórias."
    ),
)
async def create_charge(
    data: CreateChargeRequest,
    client: Annotated[PaymentClient, Depends(get_payment_client)],
) -> ChargeResponse:
    """
    Endpoint POST /api/charges/ — cria uma cobrança via PaymentClient.

    Recebe os dados da cobrança validados pelo Pydantic, delega a chamada HTTP
    ao PaymentClient injetado e retorna a ChargeResponse com status 201.

    Exceções levantadas pelo PaymentClient propagam-se automaticamente para os
    exception handlers globais em app/main.py:
        - PaymentNotFoundError      → 404 (improvável neste endpoint, mas tratado)
        - PaymentClientError        → 400
        - PaymentTimeoutError       → 504
        - PaymentUnavailableError   → 502
        - PaymentConnectionError    → 502

    Args:
        data: Dados validados da cobrança (amount, currency, description).
              Pydantic valida antes de chegar aqui — amount > 0, currency 3 chars.
        client: PaymentClient injetado via Depends(get_payment_client).
                O httpx.AsyncClient já está aberto e será fechado após o retorno.

    Returns:
        ChargeResponse com os dados completos da cobrança recém-criada (id, status,
        created_at preenchidos pela API de pagamentos).
    """
    return await client.create_charge(data)
