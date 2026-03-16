"""
Script de demonstração do client HTTP resiliente para a API de pagamentos.

Percorre todas as operações disponíveis em sequência:
    1. Setup          - exibe as configurações ativas
    2. Criar cobrança - POST /charges com sucesso
    3. Consultar      - GET /charges/{id}
    4. Listar         - GET /charges?page=1&per_page=5
    5. Reembolsar     - POST /charges/{id}/refund
    6. Retry (flaky)  - description='flaky'; mock retorna 500 nas 2 primeiras tentativas
    7. Timeout        - description='timeout'; demonstra PaymentTimeoutError
    8. Métricas       - mede latência média de N chamadas consecutivas

PRÉ-REQUISITO: o mock server deve estar rodando antes de executar este script:
    uv run uvicorn mock_server.server:app --port 8001

Execução:
    uv run python demo.py
"""

import asyncio
import sys
import time
from decimal import Decimal

# Garante que o terminal Windows use UTF-8 para exibir caracteres especiais.
# Sem isso, o PowerShell com encoding cp1252 rejeita símbolos como → e ✓.
sys.stdout.reconfigure(encoding="utf-8")

from app.client.exceptions import (
    PaymentServiceError,
    PaymentTimeoutError,
)
from app.client.payment_client import PaymentClient
from app.client.schemas import CreateChargeRequest, RefundRequest
from app.config import Settings

# ─── Utilitários de exibição ──────────────────────────────────────────────────

SEP = "=" * 64


def header(title: str) -> None:
    """Imprime um cabeçalho de seção no terminal."""
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ─── Seções da demonstração ───────────────────────────────────────────────────


async def section_setup(settings: Settings) -> None:
    """
    Exibe as configurações ativas do PaymentClient.

    Não faz chamadas HTTP, apenas mostra o que está configurado para
    ajudar a interpretar os logs que aparecem nas seções seguintes.
    """
    header("SETUP - Configurações ativas")
    print(f"  URL base:         {settings.payment_api_base_url}")
    print(f"  Connect timeout:  {settings.payment_api_connect_timeout}s")
    print(f"  Read timeout:     {settings.payment_api_read_timeout}s")
    print(f"  Max retries:      {settings.payment_api_max_retries}")
    print(f"  Backoff base:     {settings.payment_api_backoff_base}s")
    print(f"  Log format:       {settings.log_format}")


async def section_create_charge(settings: Settings) -> str:
    """
    Demonstra a criação de uma cobrança via POST /charges.

    Retorna o charge_id para uso nas seções seguintes (consultar e reembolsar).
    O Pydantic valida os dados (amount > 0, currency com 3 chars) antes de
    qualquer chamada HTTP ser feita.

    Args:
        settings: Configurações do PaymentClient.

    Returns:
        ID da cobrança criada (UUID string).
    """
    header("1. CRIAR COBRANÇA - POST /charges")

    req = CreateChargeRequest(
        amount=Decimal("150.00"),
        currency="BRL",
        description="Pedido #1001 - Demonstração do client resiliente",
    )
    print(f"\n  → Enviando: amount=R${req.amount}, currency={req.currency}")

    async with PaymentClient(settings) as client:
        charge = await client.create_charge(req)

    print(f"\n  ✓ Cobrança criada com sucesso!")
    print(f"    ID:        {charge.id}")
    print(f"    Valor:     R$ {charge.amount}")
    print(f"    Status:    {charge.status}")
    print(f"    Criada em: {charge.created_at}")

    return charge.id


async def section_get_charge(settings: Settings, charge_id: str) -> None:
    """
    Demonstra a consulta de uma cobrança por ID via GET /charges/{charge_id}.

    Usa o ID retornado pela seção 1 para buscar os dados atuais da cobrança.
    O client lança PaymentNotFoundError (HTTP 404) se o ID não existir.

    Args:
        settings: Configurações do PaymentClient.
        charge_id: ID da cobrança criada na seção anterior.
    """
    header("2. CONSULTAR COBRANÇA - GET /charges/{id}")
    print(f"\n  → Consultando ID: {charge_id}")

    async with PaymentClient(settings) as client:
        fetched = await client.get_charge(charge_id)

    print(f"\n  ✓ Cobrança encontrada!")
    print(f"    ID:        {fetched.id}")
    print(f"    Valor:     R$ {fetched.amount}")
    print(f"    Status:    {fetched.status}")
    print(f"    Descrição: {fetched.description}")


async def section_list_charges(settings: Settings) -> None:
    """
    Demonstra a listagem paginada de cobranças via GET /charges.

    Retorna ListChargesResponse com items (página atual), total e metadados
    de paginação. Aqui usamos page=1, per_page=5.

    Args:
        settings: Configurações do PaymentClient.
    """
    header("3. LISTAR COBRANÇAS - GET /charges?page=1&per_page=5")
    print("\n  → Solicitando página 1 com até 5 itens...")

    async with PaymentClient(settings) as client:
        result = await client.list_charges(page=1, per_page=5)

    total_pages = (
        (result.total + result.per_page - 1) // result.per_page
        if result.total > 0
        else 1
    )

    print(f"\n  ✓ Listagem retornada!")
    print(f"    Total de cobranças: {result.total}")
    print(f"    Página atual:       {result.page} de {total_pages}")
    print(f"    Itens nesta página: {len(result.items)}")
    print()
    for i, c in enumerate(result.items, 1):
        print(f"    [{i}] ID={c.id[:8]}... | R$ {c.amount} | status={c.status}")


async def section_refund(settings: Settings, charge_id: str) -> None:
    """
    Demonstra o reembolso total de uma cobrança via POST /charges/{id}/refund.

    Sem informar amount no RefundRequest, o reembolso é total (usa o valor
    original da cobrança). Após o reembolso, o status da cobrança muda para
    'refunded' e novas tentativas de reembolso retornam HTTP 400.

    Args:
        settings: Configurações do PaymentClient.
        charge_id: ID da cobrança a ser reembolsada.
    """
    header("4. REEMBOLSAR COBRANÇA - POST /charges/{id}/refund")

    req = RefundRequest(charge_id=charge_id)
    print(f"\n  → Solicitando reembolso total para ID: {charge_id}")

    async with PaymentClient(settings) as client:
        refund = await client.refund_charge(req)

    print(f"\n  ✓ Reembolso processado!")
    print(f"    ID do reembolso: {refund.id}")
    print(f"    Cobrança:        {refund.charge_id}")
    print(f"    Valor:           R$ {refund.amount}")
    print(f"    Status:          {refund.status}")


async def section_retry_flaky(settings: Settings) -> None:
    """
    Demonstra o retry automático com um servidor instável (flaky).

    O mock server mantém um contador por sessão. Com description='flaky':
      - 1ª chamada → HTTP 500 (falha simulada)
      - 2ª chamada → HTTP 500 (ainda falhando)
      - 3ª chamada → HTTP 201 (recuperado)

    O PaymentClient retenta automaticamente com backoff exponencial + jitter.
    Os logs mostram 'http_request_retriable_error' e 'http_request_retry_wait'
    a cada falha antes do sucesso.

    Args:
        settings: Configurações do PaymentClient.
    """
    header("5. RETRY EM AÇÃO - Servidor instável (flaky)")
    print("\n  O mock server retorna HTTP 500 nas 2 primeiras tentativas")
    print("  e HTTP 201 na 3ª. Observe os logs de retry abaixo:\n")

    req = CreateChargeRequest(
        amount=Decimal("99.90"),
        currency="BRL",
        description="flaky - teste de retry automático",
    )
    print(f"  → Enviando request com description='flaky'...")

    start = time.perf_counter()
    async with PaymentClient(settings) as client:
        charge = await client.create_charge(req)
    elapsed = time.perf_counter() - start

    print(f"\n  ✓ Sucesso após retries! (total: {elapsed:.2f}s)")
    print(f"    ID:     {charge.id}")
    print(f"    Status: {charge.status}")
    print(f"    (O backoff exponencial adicionou delay entre as tentativas)")


async def section_timeout(settings: Settings) -> None:
    """
    Demonstra o comportamento de timeout com exceção tipada capturada.

    O mock server dorme 60 segundos quando description contém 'timeout'.
    Aqui usamos um read_timeout reduzido (3s) e apenas 2 retries para
    manter a demo rápida (~9 segundos total).

    O PaymentClient levanta PaymentTimeoutError após esgotar os retries,
    carregando status_code=None (não houve resposta HTTP) e retries_attempted.

    Args:
        settings: Configurações padrão (serão sobrescritas para a demo).
    """
    header("6. TIMEOUT - Servidor sem resposta")
    print("\n  Usando read_timeout=3s e max_retries=2 para demo rápida.")
    print("  O mock server vai esperar 60s e o client vai dar timeout primeiro.\n")

    # Override de settings específico para esta demo: timeout curto + poucos retries.
    # Em produção, os valores seriam maiores (30s de read_timeout é o padrão).
    timeout_settings = Settings(
        payment_api_read_timeout=3.0,
        payment_api_max_retries=2,
        payment_api_backoff_base=0.5,
        log_format=settings.log_format,
    )

    req = CreateChargeRequest(
        amount=Decimal("50.00"),
        currency="BRL",
        description="timeout - demonstração de timeout",
    )
    print(f"  → Enviando request (timeout em {timeout_settings.payment_api_read_timeout}s)...")

    start = time.perf_counter()
    try:
        async with PaymentClient(timeout_settings) as client:
            await client.create_charge(req)
    except PaymentTimeoutError as exc:
        elapsed = time.perf_counter() - start
        print(f"\n  ✓ PaymentTimeoutError capturada como esperado! ({elapsed:.1f}s total)")
        print(f"    Tipo:      {type(exc).__name__}")
        print(f"    Mensagem:  {exc.message}")
        print(f"    Retries:   {exc.retries_attempted}")
        print(f"    Status:    {exc.status_code}  ← None = sem resposta HTTP")


async def section_metrics(settings: Settings) -> None:
    """
    Mede a latência de N cobranças consecutivas reaproveitando o mesmo client.

    Reutilizar o mesmo PaymentClient (e portanto o mesmo httpx.AsyncClient)
    aproveita o pool de conexões TCP, reduzindo overhead de handshake entre
    chamadas. Os resultados mostram latência mínima, máxima e média.

    Args:
        settings: Configurações do PaymentClient.
    """
    header("7. MÉTRICAS DE LATÊNCIA")

    n_calls = 5
    print(f"\n  → Executando {n_calls} cobranças e medindo latência de cada uma:\n")

    durations: list[float] = []

    # Reusa o mesmo client para as N chamadas (aproveita o pool de conexões)
    async with PaymentClient(settings) as client:
        for i in range(1, n_calls + 1):
            req = CreateChargeRequest(
                amount=Decimal(f"{i * 10}.00"),
                currency="BRL",
                description=f"Métrica #{i} de {n_calls}",
            )
            t_start = time.perf_counter()
            await client.create_charge(req)
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            durations.append(elapsed_ms)
            print(f"    [{i}/{n_calls}] {elapsed_ms:.1f} ms")

    avg = sum(durations) / len(durations)
    min_d = min(durations)
    max_d = max(durations)

    print(f"\n  ✓ Resultado das métricas ({n_calls} chamadas):")
    print(f"    Média:   {avg:.1f} ms")
    print(f"    Mínima:  {min_d:.1f} ms")
    print(f"    Máxima:  {max_d:.1f} ms")
    print(f"    Total:   {sum(durations):.1f} ms")


# ─── Ponto de entrada ─────────────────────────────────────────────────────────


async def main() -> None:
    """
    Executa todas as seções da demonstração em sequência.

    Usa formato de log 'console' para facilitar a leitura dos logs
    estruturados do structlog no terminal.
    """
    print(f"\n{SEP}")
    print("  CLIENT HTTP RESILIENTE - DEMO COMPLETA")
    print(SEP)
    print("\n  PRÉ-REQUISITO: mock server rodando em http://localhost:8001")
    print("    $ uv run uvicorn mock_server.server:app --port 8001\n")

    # Formato console deixa os logs do structlog legíveis no terminal
    settings = Settings(log_format="console")

    try:
        await section_setup(settings)
        charge_id = await section_create_charge(settings)
        await section_get_charge(settings, charge_id)
        await section_list_charges(settings)
        await section_refund(settings, charge_id)
        await section_retry_flaky(settings)
        await section_timeout(settings)
        await section_metrics(settings)
    except PaymentServiceError as exc:
        print(f"\n  ✗ Erro do PaymentClient: {exc}")
        print("    Verifique se o mock server está rodando na porta 8001.")
        raise
    except Exception as exc:
        print(f"\n  ✗ Erro inesperado: {type(exc).__name__}: {exc}")
        raise

    print(f"\n{SEP}")
    print("  ✓ Demo concluída com sucesso!")
    print(f"{SEP}\n")


if __name__ == "__main__":
    asyncio.run(main())
