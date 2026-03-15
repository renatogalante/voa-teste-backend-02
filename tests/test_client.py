"""
Testes das 4 operações CRUD do PaymentClient e comportamento em erros 4xx.

Usa respx para interceptar as chamadas httpx em nível de código, sem necessidade
do mock server rodando. Cobre o happy path de cada operação (create, get, list,
refund) e a falha rápida sem retry em erros 4xx (422 e 404).

Estrutura de cada teste:
  1. Define o mock da rota com respx
  2. Executa a operação no payment_client
  3. Verifica o tipo e conteúdo do retorno (ou exceção levantada)
  4. Verifica call_count quando relevante (ausência de retry)
"""

from decimal import Decimal

import httpx
import pytest
import respx

from app.client.exceptions import PaymentClientError, PaymentNotFoundError
from app.client.schemas import (
    ChargeResponse,
    CreateChargeRequest,
    ListChargesResponse,
    RefundRequest,
    RefundResponse,
)

# Payload de cobrança reutilizado nos mocks das respostas da "API externa"
CHARGE_DATA = {
    "id": "abc-123",
    "amount": "100.00",
    "currency": "BRL",
    "description": "Teste de cobrança",
    "status": "pending",
    "created_at": "2024-01-15T14:30:00+00:00",
}

# Payload de reembolso reutilizado no mock de refund
REFUND_DATA = {
    "id": "refund-456",
    "charge_id": "abc-123",
    "amount": "100.00",
    "status": "processed",
    "created_at": "2024-01-15T14:35:00+00:00",
}


@respx.mock
async def test_create_charge_success(payment_client):
    """
    Cenário: POST /charges retorna 201 com dados válidos da cobrança criada.
    Verifica que create_charge retorna ChargeResponse com todos os campos corretos,
    incluindo amount como Decimal e status='pending'.
    """
    respx.post("http://testserver/charges").mock(
        return_value=httpx.Response(201, json=CHARGE_DATA)
    )

    data = CreateChargeRequest(
        amount=Decimal("100.00"),
        currency="BRL",
        description="Teste de cobrança",
    )
    result = await payment_client.create_charge(data)

    assert isinstance(result, ChargeResponse)
    assert result.id == "abc-123"
    assert result.amount == Decimal("100.00")
    assert result.currency == "BRL"
    assert result.status == "pending"


@respx.mock
async def test_get_charge_success(payment_client):
    """
    Cenário: GET /charges/abc-123 retorna 200 com os dados da cobrança.
    Verifica que get_charge retorna ChargeResponse com o ID correto.
    """
    respx.get("http://testserver/charges/abc-123").mock(
        return_value=httpx.Response(200, json=CHARGE_DATA)
    )

    result = await payment_client.get_charge("abc-123")

    assert isinstance(result, ChargeResponse)
    assert result.id == "abc-123"
    assert result.currency == "BRL"


@respx.mock
async def test_list_charges_paginated(payment_client):
    """
    Cenário: GET /charges?page=1&per_page=10 retorna 200 com lista paginada.
    Verifica que list_charges retorna ListChargesResponse com items populados
    e metadados de paginação corretos (total, page, per_page).
    """
    list_data = {
        "items": [CHARGE_DATA],
        "total": 1,
        "page": 1,
        "per_page": 10,
    }
    respx.get("http://testserver/charges").mock(
        return_value=httpx.Response(200, json=list_data)
    )

    result = await payment_client.list_charges(page=1, per_page=10)

    assert isinstance(result, ListChargesResponse)
    assert len(result.items) == 1
    assert result.total == 1
    assert result.page == 1
    assert result.per_page == 10
    assert result.items[0].id == "abc-123"


@respx.mock
async def test_refund_charge_success(payment_client):
    """
    Cenário: POST /charges/abc-123/refund retorna 200 com dados do reembolso.
    Verifica que refund_charge retorna RefundResponse com charge_id e status corretos.
    Usa reembolso total (amount=None no RefundRequest).
    """
    respx.post("http://testserver/charges/abc-123/refund").mock(
        return_value=httpx.Response(200, json=REFUND_DATA)
    )

    data = RefundRequest(charge_id="abc-123")
    result = await payment_client.refund_charge(data)

    assert isinstance(result, RefundResponse)
    assert result.id == "refund-456"
    assert result.charge_id == "abc-123"
    assert result.amount == Decimal("100.00")
    assert result.status == "processed"


@respx.mock
async def test_client_error_no_retry_on_422(payment_client):
    """
    Cenário: POST /charges retorna 422 (Unprocessable Entity — payload inválido).
    Verifica que PaymentClientError é levantado com status_code=422.
    Verifica que o mock foi chamado EXATAMENTE 1 vez: erros 4xx são falhas
    permanentes e não disparam retry (o mesmo payload inválido geraria o mesmo erro).
    """
    route = respx.post("http://testserver/charges").mock(
        return_value=httpx.Response(422, json={"error": "Unprocessable Entity"})
    )

    data = CreateChargeRequest(
        amount=Decimal("100.00"),
        currency="BRL",
        description="Payload que a API rejeita",
    )

    with pytest.raises(PaymentClientError) as exc_info:
        await payment_client.create_charge(data)

    assert exc_info.value.status_code == 422
    assert route.call_count == 1  # sem retry para erros 4xx


@respx.mock
async def test_get_charge_not_found(payment_client):
    """
    Cenário: GET /charges/inexistente retorna 404 (recurso não encontrado).
    Verifica que PaymentNotFoundError é levantado (exceção mais específica que
    PaymentClientError, mapeada para HTTP 404 no endpoint — não 400 genérico).
    Verifica que o mock foi chamado 1 vez: 404 não dispara retry.
    """
    route = respx.get("http://testserver/charges/inexistente").mock(
        return_value=httpx.Response(404, json={"error": "Charge not found"})
    )

    with pytest.raises(PaymentNotFoundError) as exc_info:
        await payment_client.get_charge("inexistente")

    assert exc_info.value.status_code == 404
    assert route.call_count == 1  # 404 não dispara retry
