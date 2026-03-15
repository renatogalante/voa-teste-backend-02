"""
Schemas Pydantic para request e response das operações de pagamento.

Este módulo define os contratos de dados trocados entre o PaymentClient e
a API externa de pagamentos. Usar Pydantic garante validação automática
dos dados recebidos da API e dos dados enviados pelo consumidor antes
de qualquer chamada HTTP ser feita.

Decisões de design:
    - Decimal para valores monetários: evita imprecisão de ponto flutuante
      inerente ao float (ex.: 0.1 + 0.2 != 0.3 em float).
    - ConfigDict(from_attributes=True) nos models de response: permite
      instanciar a partir de objetos ORM ou dicts via model_validate(),
      além do JSON padrão — facilita testes e extensões futuras.
    - Validações de campo (gt, min_length) aplicadas na camada de entrada,
      antes de qualquer chamada à API, reduzindo round-trips desnecessários.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CreateChargeRequest(BaseModel):
    """
    Dados necessários para criar uma nova cobrança na API de pagamentos.

    Validações aplicadas antes do envio HTTP:
        - amount deve ser estritamente positivo (gt=0).
        - currency deve ter exatamente 3 caracteres (padrão ISO 4217, ex: BRL, USD).
        - description não pode ser vazia (min_length=1).

    Exemplo de uso:
        req = CreateChargeRequest(amount=Decimal("150.00"), currency="BRL", description="Pedido #42")
    """

    amount: Decimal = Field(
        gt=0,
        description="Valor da cobrança em unidades da moeda (deve ser positivo)",
    )
    currency: str = Field(
        min_length=3,
        max_length=3,
        description="Código da moeda no padrão ISO 4217 (ex: BRL, USD, EUR)",
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Descrição da cobrança exibida ao pagador",
    )


class ChargeResponse(BaseModel):
    """
    Dados retornados pela API ao criar ou consultar uma cobrança.

    O from_attributes=True permite instanciar este model a partir de objetos
    que expõem atributos (ex: ORMs, dataclasses) além de dicionários comuns.

    O campo status representa o ciclo de vida da cobrança:
        - "pending"  → aguardando pagamento
        - "paid"     → pagamento confirmado
        - "refunded" → reembolso processado
        - "failed"   → falha no processamento
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    amount: Decimal
    currency: str
    description: str
    status: str
    created_at: datetime


class ListChargesResponse(BaseModel):
    """
    Resposta paginada da listagem de cobranças.

    Encapsula tanto os itens da página corrente quanto metadados de paginação,
    permitindo ao consumidor navegar pelos resultados sem chamadas extras
    para obter o total de registros.
    """

    items: list[ChargeResponse]
    total: int
    page: int
    per_page: int


class RefundRequest(BaseModel):
    """
    Dados necessários para solicitar reembolso de uma cobrança.

    O campo amount é opcional: quando None, a API processa reembolso total.
    Quando informado, deve ser positivo e menor ou igual ao valor original
    (validação do limite cabe à API, não a este schema).

    Exemplo de uso:
        # Reembolso total
        req = RefundRequest(charge_id="abc-123")

        # Reembolso parcial de R$ 50,00
        req = RefundRequest(charge_id="abc-123", amount=Decimal("50.00"))
    """

    charge_id: str
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        description="Valor a reembolsar (positivo); None para reembolso total",
    )


class RefundResponse(BaseModel):
    """
    Dados retornados pela API ao processar um reembolso.

    O campo status representa a situação do reembolso:
        - "pending"   → reembolso registrado, aguardando processamento
        - "processed" → reembolso concluído com sucesso

    O from_attributes=True segue o mesmo padrão de ChargeResponse para
    consistência e compatibilidade futura com camadas de persistência.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    charge_id: str
    amount: Decimal
    status: str
    created_at: datetime
