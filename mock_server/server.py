"""
Mock server para simular a API externa de pagamentos.

Este módulo implementa um servidor FastAPI que imita o comportamento da API de
pagamentos fictícia para fins de desenvolvimento e testes locais. Roda na porta
8001 e armazena todos os dados em memória (os dados são perdidos ao reiniciar).

Endpoints disponíveis:
    POST   /charges               → Cria uma cobrança
    GET    /charges/{charge_id}   → Consulta uma cobrança por ID
    GET    /charges               → Lista cobranças (paginado)
    POST   /charges/{charge_id}/refund → Reembolsa uma cobrança

Comportamentos especiais (baseados no campo description do POST /charges):
    - Contém "timeout"   → asyncio.sleep(60) para simular timeout do client
    - Contém "error_500" → retorna HTTP 500 imediatamente
    - Contém "flaky"     → retorna HTTP 500 nas 2 primeiras chamadas,
                           HTTP 201 na 3ª (útil para testar retry automático)

Os formatos de resposta são idênticos aos schemas definidos em
app/client/schemas.py, garantindo compatibilidade com o PaymentClient.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# Aplicação FastAPI
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Mock Payment API",
    description=(
        "Servidor mock para simulação da API de pagamentos. "
        "Usado em desenvolvimento e testes do PaymentClient resiliente."
    ),
    version="1.0.0",
)

# ─────────────────────────────────────────────────────────────────────────────
# Armazenamento em memória
# ─────────────────────────────────────────────────────────────────────────────

# "Banco de dados" em memória: chave = charge_id (UUID str), valor = dict da cobrança
charges_db: dict[str, dict] = {}

# Contador global de chamadas com description "flaky".
# Chave: string fixa "flaky" (há apenas um contador compartilhado por sessão).
# Valor: número de chamadas recebidas desde o último reset.
flaky_counter: dict[str, int] = {}

# ─────────────────────────────────────────────────────────────────────────────
# Schemas de request (internos ao mock server)
# ─────────────────────────────────────────────────────────────────────────────


class CreateChargeBody(BaseModel):
    """
    Corpo do request para criação de cobrança.

    Espelha os campos de CreateChargeRequest do PaymentClient para garantir
    que o mock aceite exatamente os mesmos dados que o client envia.
    """

    amount: Decimal = Field(gt=0, description="Valor da cobrança (deve ser positivo)")
    currency: str = Field(min_length=3, max_length=3, description="Moeda ISO 4217")
    description: str = Field(min_length=1, max_length=500, description="Descrição")


class RefundBody(BaseModel):
    """
    Corpo do request para reembolso de cobrança.

    O campo amount é opcional: quando ausente, o mock processa reembolso total
    (usa o valor integral da cobrança original como montante do reembolso).
    """

    amount: Decimal | None = Field(
        default=None,
        gt=0,
        description="Valor a reembolsar; None ou ausente = reembolso total",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Função auxiliar para formatar cobrança como dict serializável
# ─────────────────────────────────────────────────────────────────────────────


def _charge_to_dict(charge: dict) -> dict:
    """
    Converte o dict interno de uma cobrança para formato serializável em JSON.

    O campo amount é armazenado internamente como str (Decimal serializado)
    para evitar perda de precisão. Este helper garante que o formato retornado
    seja compatível com ChargeResponse do PaymentClient.

    Args:
        charge: Dict interno da cobrança (como armazenado em charges_db).

    Returns:
        Dict pronto para serialização JSON, com amount como string.
    """
    return {
        "id": charge["id"],
        "amount": charge["amount"],
        "currency": charge["currency"],
        "description": charge["description"],
        "status": charge["status"],
        "created_at": charge["created_at"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/charges", status_code=201)
async def create_charge(body: CreateChargeBody) -> JSONResponse:
    """
    Cria uma nova cobrança e a armazena em memória.

    Verifica o campo description para acionar comportamentos especiais antes
    de persistir e retornar a cobrança criada.

    Comportamentos especiais (verificados nesta ordem):
        1. "timeout"   → aguarda 60s antes de responder (simula timeout do client)
        2. "error_500" → retorna HTTP 500 imediatamente (sem criar a cobrança)
        3. "flaky"     → incrementa contador; retorna 500 nas 2 primeiras chamadas
                         e 201 na 3ª (o contador é resetado após o sucesso)

    Args:
        body: Dados validados da cobrança (amount, currency, description).

    Returns:
        JSONResponse 201 com dados da cobrança criada (alinhado com ChargeResponse),
        ou JSONResponse 500 nos cenários de erro simulado.
    """
    description: str = body.description

    # ── Comportamento "timeout": simula servidor lento ─────────────────────
    # O PaymentClient tem read_timeout configurado (ex: 30s). Com sleep(60),
    # qualquer chamada que contenha "timeout" na description vai expirar o
    # timeout do client, acionando o fluxo de retry e finalmente levantando
    # PaymentTimeoutError (mapeado para HTTP 504 no endpoint principal).
    if "timeout" in description:
        await asyncio.sleep(60)

    # ── Comportamento "error_500": simula falha interna do servidor ────────
    # Permite testar o retry automático do PaymentClient e a exceção
    # PaymentUnavailableError após esgotar as tentativas.
    if "error_500" in description:
        return JSONResponse(
            status_code=500,
            content={"error": "Internal Server Error"},
        )

    # ── Comportamento "flaky": falha nas N primeiras chamadas ──────────────
    # Simula um servidor instável que eventualmente se recupera.
    # O contador é global para a sessão do servidor (não por cliente).
    # Útil para validar que o retry automático consegue sucesso na 3ª tentativa.
    if "flaky" in description:
        flaky_counter["flaky"] = flaky_counter.get("flaky", 0) + 1
        call_count: int = flaky_counter["flaky"]

        if call_count < 3:
            # Primeira e segunda chamadas: retorna 500 para disparar retry
            return JSONResponse(
                status_code=500,
                content={"error": f"Internal Server Error (flaky attempt {call_count}/2)"},
            )

        # Terceira chamada em diante: reseta o contador e prossegue para o 201
        flaky_counter["flaky"] = 0

    # ── Criação normal da cobrança ─────────────────────────────────────────
    charge_id: str = str(uuid.uuid4())
    now_iso: str = datetime.now(timezone.utc).isoformat()

    # Converte Decimal para str para serialização JSON sem perda de precisão.
    # O PaymentClient reconstrói o Decimal a partir da string via Pydantic.
    charge: dict = {
        "id": charge_id,
        "amount": str(body.amount),
        "currency": body.currency,
        "description": description,
        "status": "pending",
        "created_at": now_iso,
    }
    charges_db[charge_id] = charge

    return JSONResponse(status_code=201, content=_charge_to_dict(charge))


@app.get("/charges/{charge_id}")
async def get_charge(charge_id: str) -> JSONResponse:
    """
    Retorna os dados atuais de uma cobrança pelo seu ID.

    Args:
        charge_id: UUID da cobrança (gerado no POST /charges).

    Returns:
        JSONResponse 200 com dados da cobrança (alinhado com ChargeResponse),
        ou JSONResponse 404 se o ID não existir em charges_db.
    """
    charge = charges_db.get(charge_id)

    if charge is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Charge not found"},
        )

    return JSONResponse(status_code=200, content=_charge_to_dict(charge))


@app.get("/charges")
async def list_charges(page: int = 1, per_page: int = 10) -> JSONResponse:
    """
    Lista cobranças armazenadas em memória com paginação offset-based.

    A paginação é calculada sobre os valores do dict charges_db na ordem de
    inserção (Python 3.7+ garante que dicts mantêm ordem de inserção).

    Args:
        page: Número da página solicitada (começa em 1). Default: 1.
        per_page: Quantidade máxima de itens por página. Default: 10.

    Returns:
        JSONResponse 200 com estrutura compatível com ListChargesResponse:
        items (lista da página), total (total de cobranças), page e per_page.
    """
    all_charges: list[dict] = list(charges_db.values())
    total: int = len(all_charges)

    # Calcula os índices de início e fim para o slice da página solicitada
    start: int = (page - 1) * per_page
    end: int = start + per_page
    page_items: list[dict] = [_charge_to_dict(c) for c in all_charges[start:end]]

    return JSONResponse(
        status_code=200,
        content={
            "items": page_items,
            "total": total,
            "page": page,
            "per_page": per_page,
        },
    )


@app.post("/charges/{charge_id}/refund")
async def refund_charge(charge_id: str, request: Request) -> JSONResponse:
    """
    Processa o reembolso (total ou parcial) de uma cobrança existente.

    O body é lido manualmente via Request para suportar requisições com body
    vazio (reembolso total sem enviar amount). Quando amount está presente,
    é usado como valor do reembolso; quando ausente, usa o valor integral
    da cobrança original.

    Args:
        charge_id: UUID da cobrança a ser reembolsada.
        request: Request FastAPI, lido manualmente para suportar body opcional.

    Returns:
        JSONResponse 200 com dados do reembolso (alinhado com RefundResponse),
        JSONResponse 400 se a cobrança já foi reembolsada,
        JSONResponse 404 se o charge_id não existir.
    """
    # ── Valida existência da cobrança ──────────────────────────────────────
    charge = charges_db.get(charge_id)
    if charge is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Charge not found"},
        )

    # ── Valida que a cobrança ainda não foi reembolsada ────────────────────
    if charge["status"] == "refunded":
        return JSONResponse(
            status_code=400,
            content={"error": "Charge already refunded"},
        )

    # ── Lê o body (pode ser vazio para reembolso total) ────────────────────
    # Usamos request.json() com fallback para dict vazio quando body está ausente
    # ou não é JSON válido, para aceitar requests sem Content-Type: application/json.
    try:
        raw_body: dict = await request.json()
    except Exception:
        raw_body = {}

    # Determina o valor do reembolso: parcial (se amount enviado) ou total
    refund_amount_raw = raw_body.get("amount")
    if refund_amount_raw is not None:
        refund_amount: str = str(Decimal(str(refund_amount_raw)))
    else:
        # Reembolso total: usa o valor original da cobrança
        refund_amount = charge["amount"]

    # ── Atualiza status da cobrança para "refunded" ────────────────────────
    charge["status"] = "refunded"
    charges_db[charge_id] = charge

    # ── Constrói e retorna a resposta de reembolso ─────────────────────────
    # Formato alinhado com RefundResponse do PaymentClient
    refund: dict = {
        "id": str(uuid.uuid4()),
        "charge_id": charge_id,
        "amount": refund_amount,
        "status": "processed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return JSONResponse(status_code=200, content=refund)
