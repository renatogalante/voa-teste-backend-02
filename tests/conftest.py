"""
Fixtures compartilhadas para os testes do PaymentClient.

Define as configurações de teste (Settings com valores mínimos para rapidez)
e o fixture assíncrono do PaymentClient, reutilizados em todos os módulos de teste.
O uso de backoff_base=0.01 garante que o delay entre retries seja praticamente zero,
tornando os testes de retry rápidos sem comprometer a validade da lógica.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from app.client.payment_client import PaymentClient
from app.config import Settings


@pytest.fixture
def test_settings() -> Settings:
    """
    Settings configuradas para execução dos testes.

    Usa http://testserver como base_url — URL que o respx intercepta para mockar
    as chamadas httpx sem precisar de servidor real rodando.

    O backoff_base=0.01 (10ms) reduz o delay entre retries de ~1s para ~10ms,
    tornando os testes de retry praticamente instantâneos.
    """
    return Settings(
        payment_api_base_url="http://testserver",
        payment_api_connect_timeout=1.0,
        payment_api_read_timeout=2.0,
        payment_api_max_retries=3,
        payment_api_backoff_base=0.01,
    )


@pytest_asyncio.fixture
async def payment_client(test_settings: Settings) -> AsyncGenerator[PaymentClient, None]:
    """
    PaymentClient inicializado dentro de async context manager para os testes.

    O context manager garante que o httpx.AsyncClient seja aberto antes do
    yield (pré-condição para fazer requests) e fechado após cada teste,
    mesmo em caso de falha, evitando leakage de conexões entre testes.

    Usa test_settings para apontar para http://testserver, que é interceptado
    pelo respx em cada teste individual.
    """
    async with PaymentClient(test_settings) as client:
        yield client
