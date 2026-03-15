"""
Testes integrados do endpoint POST /api/charges/ da aplicação FastAPI.

Usa httpx.AsyncClient com ASGITransport para exercitar a app FastAPI diretamente
em processo, sem servidor rodando. O respx intercepta as chamadas HTTP que o
PaymentClient faz à API externa (http://testserver), isolando completamente o teste.

Estratégia de isolamento:
  - app.dependency_overrides substitui get_payment_client pelo override de teste,
    que usa test_settings (base_url=http://testserver, backoff_base=0.01).
  - respx.mock intercepta as chamadas httpx ao http://testserver e retorna respostas
    controladas, permitindo testar todos os cenários sem infraestrutura externa.
  - O fixture app_with_overrides limpa dependency_overrides após cada teste para
    evitar contaminação entre testes que compartilham a mesma instância do app.

Cenários cobertos:
  1. Sucesso (201): fluxo completo FastAPI → PaymentClient → API mocked → resposta
  2. Input inválido (422): validação Pydantic falha antes de chegar ao endpoint
  3. Timeout (504): PaymentTimeoutError mapeado pelo exception handler global
  4. Indisponível (502): PaymentUnavailableError mapeado para 502 após retries
  5. Not Found (404): PaymentNotFoundError mapeado para 404 (não 400 genérico)
"""

from collections.abc import AsyncGenerator

import httpx
import pytest
import respx

from app.client.payment_client import PaymentClient
from app.config import Settings

# Payload retornado pelo mock da "API externa" no cenário de sucesso
CHARGE_DATA = {
    "id": "endpoint-charge",
    "amount": "200.00",
    "currency": "BRL",
    "description": "Teste via endpoint",
    "status": "pending",
    "created_at": "2024-01-15T16:00:00+00:00",
}


def _raise_read_timeout(request: httpx.Request) -> None:
    """
    Efeito colateral que simula timeout de leitura na chamada à API externa.

    Args:
        request: Request httpx interceptado pelo respx.

    Raises:
        httpx.ReadTimeout: sempre, simulando timeout de leitura.
    """
    raise httpx.ReadTimeout("Simulated timeout", request=request)


@pytest.fixture
def app_with_overrides(test_settings: Settings):
    """
    Fixture que sobrescreve get_payment_client na app FastAPI para usar test_settings.

    Sem o override, o endpoint chamaria get_settings() (via lru_cache) que retornaria
    as settings do .env real (base_url=http://localhost:8001). Com o override, o
    PaymentClient usa test_settings (base_url=http://testserver), que o respx intercepta.

    O dependency_overrides.clear() ao final garante que outros testes não herdem
    o override caso compartilhem a mesma instância do app.

    Yields:
        Instância do FastAPI app com a dependency sobrescrita para testes.
    """
    from app.api.charges import get_payment_client
    from app.main import app

    async def override_get_payment_client() -> AsyncGenerator[PaymentClient, None]:
        """Versão de teste do get_payment_client que usa test_settings."""
        async with PaymentClient(test_settings) as client:
            yield client

    app.dependency_overrides[get_payment_client] = override_get_payment_client
    yield app
    # Limpa o override após o teste para não contaminar outros
    app.dependency_overrides.clear()


@respx.mock
async def test_create_charge_endpoint_success(app_with_overrides):
    """
    Cenário: POST /api/charges/ com body válido; API externa retorna 201.

    Valida o fluxo completo: FastAPI recebe o request → valida o body com Pydantic →
    injeta PaymentClient via Depends → PaymentClient chama http://testserver/charges
    (interceptado pelo respx) → endpoint retorna HTTP 201 com ChargeResponse.
    """
    respx.post("http://testserver/charges").mock(
        return_value=httpx.Response(201, json=CHARGE_DATA)
    )

    transport = httpx.ASGITransport(app=app_with_overrides)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/api/charges/",
            json={"amount": "200.00", "currency": "BRL", "description": "Teste via endpoint"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "endpoint-charge"
    assert body["status"] == "pending"
    assert body["currency"] == "BRL"


async def test_invalid_input_returns_422(app_with_overrides):
    """
    Cenário: POST /api/charges/ com body vazio (sem campos obrigatórios).

    O Pydantic valida o body antes de chamar o endpoint — nenhuma chamada HTTP
    externa ocorre. A validação falha e o FastAPI retorna HTTP 422 automaticamente
    com os detalhes dos campos ausentes ou inválidos.

    Não usa @respx.mock porque nenhuma chamada httpx é feita neste fluxo.
    """
    transport = httpx.ASGITransport(app=app_with_overrides)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post("/api/charges/", json={})

    assert response.status_code == 422


@respx.mock
async def test_upstream_timeout_returns_504(app_with_overrides):
    """
    Cenário: API externa causa ReadTimeout em todas as tentativas de retry.

    O PaymentClient esgota max_retries e levanta PaymentTimeoutError.
    O exception handler global em app/main.py captura PaymentTimeoutError
    e retorna HTTP 504 Gateway Timeout para o consumidor.
    """
    respx.post("http://testserver/charges").mock(side_effect=_raise_read_timeout)

    transport = httpx.ASGITransport(app=app_with_overrides)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/api/charges/",
            json={"amount": "100.00", "currency": "BRL", "description": "timeout"},
        )

    assert response.status_code == 504


@respx.mock
async def test_upstream_unavailable_returns_502(app_with_overrides):
    """
    Cenário: API externa retorna 500 em todas as 3 tentativas de retry.

    O PaymentClient esgota max_retries e levanta PaymentUnavailableError.
    O exception handler global em app/main.py captura PaymentUnavailableError
    e retorna HTTP 502 Bad Gateway, indicando que o upstream está fora.
    """
    respx.post("http://testserver/charges").mock(
        side_effect=[
            httpx.Response(500, json={"error": "Internal Server Error"}),
            httpx.Response(500, json={"error": "Internal Server Error"}),
            httpx.Response(500, json={"error": "Internal Server Error"}),
        ]
    )

    transport = httpx.ASGITransport(app=app_with_overrides)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/api/charges/",
            json={"amount": "100.00", "currency": "BRL", "description": "error_500"},
        )

    assert response.status_code == 502


@respx.mock
async def test_charge_not_found_returns_404(app_with_overrides):
    """
    Cenário: API externa retorna 404 (recurso não encontrado) ao criar cobrança.

    O PaymentClient levanta PaymentNotFoundError (herda de PaymentClientError).
    O exception handler específico para PaymentNotFoundError — registrado em
    app/main.py ANTES do handler genérico de PaymentClientError — captura a
    exceção e retorna HTTP 404 Not Found (não 400 Bad Request).

    Este teste valida a ordem correta de registro dos exception handlers:
    o mais específico (NotFoundError → 404) deve vencer o mais genérico
    (ClientError → 400) quando a hierarquia de herança está em jogo.
    """
    respx.post("http://testserver/charges").mock(
        return_value=httpx.Response(404, json={"error": "Charge not found"})
    )

    transport = httpx.ASGITransport(app=app_with_overrides)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/api/charges/",
            json={"amount": "100.00", "currency": "BRL", "description": "not found"},
        )

    assert response.status_code == 404
