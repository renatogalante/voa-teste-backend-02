"""
Testes do comportamento de timeout do PaymentClient.

Valida que o PaymentClient trata httpx.TimeoutException como erro transitório,
ativando o mecanismo de retry — e que levanta PaymentTimeoutError apenas após
esgotar todas as tentativas configuradas (max_retries=3).

Dois cenários cobertos:
  1. Timeout em todas as tentativas → PaymentTimeoutError após max_retries
  2. Timeout nas primeiras tentativas, sucesso na última → retorna ChargeResponse

O helper _raise_read_timeout é um callable (side_effect) que simula a exceção
httpx.ReadTimeout que o httpx levantaria num timeout de leitura real.
"""

from decimal import Decimal

import httpx
import pytest
import respx

from app.client.exceptions import PaymentTimeoutError
from app.client.schemas import ChargeResponse, CreateChargeRequest

# Payload de cobrança retornado na tentativa bem-sucedida após timeouts
CHARGE_DATA = {
    "id": "timeout-charge",
    "amount": "75.00",
    "currency": "BRL",
    "description": "Cobrança após timeout",
    "status": "pending",
    "created_at": "2024-01-15T15:00:00+00:00",
}


def _raise_read_timeout(request: httpx.Request) -> None:
    """
    Efeito colateral que simula um timeout de leitura do httpx.

    Usado como callable em respx.mock(side_effect=...) para que o respx
    chame esta função com o request interceptado e ela levante a exceção.
    ReadTimeout herda de TimeoutException, que o PaymentClient captura
    e trata como erro transitório elegível para retry.

    Args:
        request: Request httpx interceptado pelo respx (passado automaticamente).

    Raises:
        httpx.ReadTimeout: sempre, simulando timeout de leitura.
    """
    raise httpx.ReadTimeout("Simulated read timeout", request=request)


@respx.mock
async def test_timeout_raises_error(payment_client):
    """
    Cenário: Todas as 3 tentativas (max_retries=3) resultam em ReadTimeout.

    O PaymentClient retenta cada timeout (comportamento transitório) e só
    desiste após esgotar max_retries. Ao esgotar, levanta PaymentTimeoutError
    porque o último erro registrado foi uma httpx.TimeoutException.

    Confirma que o mock foi chamado 3 vezes (uma por tentativa),
    provando que o client não desistiu prematuramente.
    """
    route = respx.post("http://testserver/charges").mock(
        side_effect=_raise_read_timeout
    )

    data = CreateChargeRequest(
        amount=Decimal("75.00"),
        currency="BRL",
        description="Sempre em timeout",
    )

    with pytest.raises(PaymentTimeoutError) as exc_info:
        await payment_client.create_charge(data)

    assert exc_info.value.retries_attempted == 3
    assert route.call_count == 3  # retentou 3 vezes antes de desistir


@respx.mock
async def test_timeout_then_success(payment_client):
    """
    Cenário: As 2 primeiras chamadas resultam em ReadTimeout; a 3ª retorna 201.

    Demonstra que timeout é tratado como erro transitório: o client retenta
    e, ao obter sucesso na 3ª tentativa, retorna ChargeResponse normalmente.
    Útil para simular upstream lento que eventualmente responde.

    Confirma que o mock foi chamado 3 vezes (2 timeouts + 1 sucesso).
    """
    route = respx.post("http://testserver/charges").mock(
        side_effect=[
            _raise_read_timeout,
            _raise_read_timeout,
            httpx.Response(201, json=CHARGE_DATA),
        ]
    )

    data = CreateChargeRequest(
        amount=Decimal("75.00"),
        currency="BRL",
        description="Timeout depois sucesso",
    )
    result = await payment_client.create_charge(data)

    assert isinstance(result, ChargeResponse)
    assert result.id == "timeout-charge"
    assert result.amount == Decimal("75.00")
    assert route.call_count == 3  # 2 timeouts + 1 sucesso
