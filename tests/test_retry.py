"""
Testes do mecanismo de retry com backoff exponencial do PaymentClient.

Valida três cenários centrais da resiliência do client:
  1. Retry com sucesso: upstream flaky retorna 500 nas primeiras tentativas e
     200 na última — PaymentClient persiste e retorna o resultado com sucesso.
  2. Retry esgotado: todas as tentativas (max_retries=3) falham com 5xx —
     PaymentUnavailableError é levantado com a contagem correta de retries.
  3. Sem retry em 4xx: o client falha imediatamente em erros do cliente,
     sem desperdiçar tentativas em algo que não vai se resolver.

Os testes usam backoff_base=0.01 (via test_settings) para que o delay entre
retries seja ~10ms em vez de ~1s, sem comprometer a validade da lógica.
"""

from decimal import Decimal

import httpx
import pytest
import respx

from app.client.exceptions import PaymentClientError, PaymentUnavailableError
from app.client.schemas import ChargeResponse, CreateChargeRequest

# Payload de cobrança para o mock da "API externa" no cenário de sucesso
CHARGE_DATA = {
    "id": "charge-retry",
    "amount": "50.00",
    "currency": "BRL",
    "description": "Cobrança após retries",
    "status": "pending",
    "created_at": "2024-01-15T14:30:00+00:00",
}


@respx.mock
async def test_retry_on_500_then_success(payment_client):
    """
    Cenário: As 2 primeiras chamadas retornam 500; a 3ª retorna 201 (sucesso).

    Simula um upstream "flaky" que se recupera na terceira tentativa.
    O PaymentClient deve retentar automaticamente e retornar ChargeResponse
    com os dados da resposta bem-sucedida.

    Confirma que o mock foi chamado exatamente 3 vezes (2 falhas + 1 sucesso),
    provando que os retries ocorreram antes do sucesso.
    """
    route = respx.post("http://testserver/charges").mock(
        side_effect=[
            httpx.Response(500, json={"error": "Internal Server Error"}),
            httpx.Response(500, json={"error": "Internal Server Error"}),
            httpx.Response(201, json=CHARGE_DATA),
        ]
    )

    data = CreateChargeRequest(
        amount=Decimal("50.00"),
        currency="BRL",
        description="Cobrança após retries",
    )
    result = await payment_client.create_charge(data)

    assert isinstance(result, ChargeResponse)
    assert result.id == "charge-retry"
    assert route.call_count == 3  # 2 tentativas com 500 + 1 sucesso com 201


@respx.mock
async def test_retry_exhausted_raises_unavailable(payment_client):
    """
    Cenário: Todas as 3 tentativas (max_retries=3) retornam 500.

    O PaymentClient esgota todos os retries e levanta PaymentUnavailableError,
    que carrega o status_code do último erro (500) e o número total de tentativas.

    Confirma que o mock foi chamado exatamente 3 vezes, provando que o client
    não desistiu antes de esgotar todas as tentativas configuradas.
    """
    route = respx.post("http://testserver/charges").mock(
        side_effect=[
            httpx.Response(500, json={"error": "Internal Server Error"}),
            httpx.Response(500, json={"error": "Internal Server Error"}),
            httpx.Response(500, json={"error": "Internal Server Error"}),
        ]
    )

    data = CreateChargeRequest(
        amount=Decimal("50.00"),
        currency="BRL",
        description="Upstream sempre indisponível",
    )

    with pytest.raises(PaymentUnavailableError) as exc_info:
        await payment_client.create_charge(data)

    assert exc_info.value.retries_attempted == 3
    assert exc_info.value.status_code == 500
    assert route.call_count == 3  # todas as tentativas foram feitas


@respx.mock
async def test_no_retry_on_4xx(payment_client):
    """
    Cenário: POST /charges retorna 400 Bad Request (erro do cliente).

    Erros 4xx (exceto 429) representam falhas permanentes no request — o mesmo
    payload inválido geraria o mesmo erro em novas tentativas. Por isso, o client
    deve falhar imediatamente sem retentar.

    Verifica que:
      - PaymentClientError é levantado com status_code=400
      - O mock foi chamado EXATAMENTE 1 vez (sem retry)
    """
    route = respx.post("http://testserver/charges").mock(
        return_value=httpx.Response(400, json={"error": "Bad Request"})
    )

    data = CreateChargeRequest(
        amount=Decimal("50.00"),
        currency="BRL",
        description="Request inválido",
    )

    with pytest.raises(PaymentClientError) as exc_info:
        await payment_client.create_charge(data)

    assert exc_info.value.status_code == 400
    assert route.call_count == 1  # sem retry: 4xx é falha permanente
