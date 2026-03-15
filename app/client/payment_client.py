"""
Client HTTP resiliente para a API de pagamentos.

Este módulo implementa o PaymentClient — o núcleo da aplicação. Toda comunicação
com a API externa de pagamentos passa por aqui. As responsabilidades são:

  - Gerenciar o ciclo de vida do httpx.AsyncClient via async context manager,
    garantindo que o pool de conexões seja aberto e fechado corretamente.
  - Executar chamadas HTTP com retry automático para falhas transitórias,
    usando backoff exponencial com jitter para evitar thundering herd.
  - Respeitar timeouts configuráveis (connect e read) via httpx.Timeout.
  - Produzir logs estruturados em JSON a cada request/response/erro usando structlog.
  - Converter respostas JSON em modelos Pydantic tipados (ChargeResponse, etc.).
  - Mapear status HTTP e exceções do httpx na hierarquia de exceções interna
    (PaymentTimeoutError, PaymentUnavailableError, etc.).

Decisões de design:
  - httpx.AsyncClient é preferível a criar uma nova conexão por chamada: o pool
    de conexões reutiliza TCP/TLS entre requests, reduzindo latência e overhead.
  - Retry somente para erros transitórios (5xx, 429, timeout, connect error).
    Erros 4xx (exceto 429) indicam problema no request — retentar não os resolve.
  - Backoff exponencial com jitter: sem jitter, múltiplos clients retentando ao
    mesmo tempo poderiam sincronizar e sobrecarregar o servidor no mesmo instante.
  - structlog produz JSON estruturado pronto para ingestão em sistemas de
    observabilidade (Datadog, Loki, Grafana), com campos padronizados por evento.
  - time.perf_counter() é mais preciso que time.time() para medir duração de
    operações curtas, pois usa o contador de alta resolução do sistema operacional.
"""

import asyncio
import random
import time
from typing import Any

import httpx
import structlog

from app.client.exceptions import (
    PaymentClientError,
    PaymentConnectionError,
    PaymentNotFoundError,
    PaymentTimeoutError,
    PaymentUnavailableError,
)
from app.client.schemas import (
    ChargeResponse,
    CreateChargeRequest,
    ListChargesResponse,
    RefundRequest,
    RefundResponse,
)
from app.config import Settings


def _configure_structlog(log_format: str) -> None:
    """
    Configura o structlog com os processadores adequados ao ambiente.

    Chamado no __init__ do PaymentClient para garantir que os logs produzidos
    estejam no formato correto desde o primeiro request.

    Em produção (log_format='json'), o JSONRenderer gera linhas JSON compactas
    prontas para ingestão em sistemas de observabilidade (Datadog, Loki, etc.).
    Em desenvolvimento (log_format='console'), o ConsoleRenderer exibe logs
    com cores ANSI e formatação legível por humanos.

    A configuração é idempotente — chamar múltiplas vezes não duplica processadores
    porque structlog.configure() substitui a configuração anterior.

    Args:
        log_format: 'json' para saída JSON estruturada ou qualquer outro valor
                    para saída no formato console colorido.
    """
    # Escolhe o renderizador final baseado no formato configurado nas settings
    if log_format == "console":
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            # Adiciona o nível de log (info, warning, error) ao dicionário de contexto
            structlog.stdlib.add_log_level,
            # Adiciona timestamp no formato ISO 8601 (ex: "2024-01-15T14:30:00.123Z")
            structlog.processors.TimeStamper(fmt="iso"),
            # Renderiza o evento final no formato escolhido (JSON ou console)
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Evita re-lookup do logger em chamadas frequentes — melhora performance
        cache_logger_on_first_use=True,
    )


class PaymentClient:
    """
    Client HTTP resiliente para a API de pagamentos.

    Encapsula todas as chamadas à API externa, adicionando retry automático,
    timeout configurável e logging estruturado. Os quatro métodos públicos
    (create_charge, get_charge, list_charges, refund_charge) convertem JSON
    em modelos Pydantic tipados e propagam exceções semânticas.

    DEVE ser usado como async context manager:

        async with PaymentClient(settings) as client:
            charge = await client.create_charge(data)

    O context manager é obrigatório porque o httpx.AsyncClient abre um pool
    de conexões que precisa ser explicitamente fechado para liberar recursos.
    Instanciar sem o context manager deixaria sockets abertos indefinidamente.

    Attributes:
        _settings: Configurações da aplicação (URL base, timeouts, max retries, etc.).
        _client: httpx.AsyncClient criado em __aenter__ e fechado em __aexit__.
        _log: Logger structlog vinculado ao contexto 'PaymentClient'.
    """

    def __init__(self, settings: Settings) -> None:
        """
        Inicializa o PaymentClient com as configurações fornecidas.

        O httpx.AsyncClient NÃO é criado aqui — isso ocorre em __aenter__ para
        garantir que o pool de conexões exista apenas dentro do bloco `async with`.
        Criar o client aqui sem context manager deixaria conexões abertas
        sem possibilidade de fechamento controlado.

        Configura o structlog aqui para que os logs estejam no formato correto
        mesmo antes do primeiro request.

        Args:
            settings: Instância de Settings com URL base, timeouts, max_retries,
                      backoff_base e formato de log.
        """
        self._settings = settings

        # Configura o structlog com o formato definido nas settings.
        # Isso afeta todos os loggers da aplicação — é intencional, pois
        # o formato deve ser consistente em todos os módulos.
        _configure_structlog(settings.log_format)

        # Cria um logger com contexto fixo 'client=PaymentClient'.
        # O bind() adiciona campos permanentes que aparecerão em todos os logs
        # produzidos por esta instância, facilitando o filtro em sistemas de log.
        self._log = structlog.get_logger().bind(client="PaymentClient")

        # Placeholder: o AsyncClient real é criado em __aenter__
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "PaymentClient":
        """
        Cria o httpx.AsyncClient ao entrar no bloco async with.

        O Timeout é configurado com dois valores separados porque connect e read
        têm semânticas distintas:
          - connect: tempo máximo para estabelecer a conexão TCP/TLS com o servidor.
            Falha rápida aqui indica problema de rede ou DNS.
          - read: tempo máximo para receber os dados da resposta após a conexão.
            Falha aqui indica servidor lento ou travado processando o request.

        Usar um timeout único para os dois mascararia a causa raiz do problema.

        Returns:
            A própria instância de PaymentClient para ser usada no bloco with.
        """
        # Timeout separado para conexão (TCP/TLS) e leitura (dados da resposta).
        # O write timeout não é configurado — para pagamentos o gargalo costuma
        # ser a leitura, não a escrita.
        timeout = httpx.Timeout(
            connect=self._settings.payment_api_connect_timeout,
            read=self._settings.payment_api_read_timeout,
        )

        # base_url garante que todos os requests usem a URL da API configurada
        # sem repeti-la a cada chamada. O httpx concatena automaticamente com o path.
        self._client = httpx.AsyncClient(
            base_url=self._settings.payment_api_base_url,
            timeout=timeout,
        )

        self._log.info(
            "payment_client_started",
            base_url=self._settings.payment_api_base_url,
            connect_timeout=self._settings.payment_api_connect_timeout,
            read_timeout=self._settings.payment_api_read_timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """
        Fecha o httpx.AsyncClient ao sair do bloco async with.

        O aclose() drena o pool de conexões e libera os sockets associados,
        mesmo que o bloco with tenha encerrado com uma exceção. Sem isso,
        conexões abertas vazariam entre requests ou ao encerrar a aplicação,
        causando warnings do asyncio sobre tasks/resources não fechados.

        Args:
            exc_type: Tipo da exceção que encerrou o bloco, ou None se não houve.
            exc_val: Instância da exceção, ou None.
            exc_tb: Traceback da exceção, ou None.
        """
        if self._client is not None:
            await self._client.aclose()
            self._log.info("payment_client_closed")

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS PÚBLICOS
    # Cada método representa uma operação na API de pagamentos.
    # Todos delegam o trabalho de HTTP + retry para _request() e convertem
    # a resposta JSON em um modelo Pydantic tipado.
    # ─────────────────────────────────────────────────────────────────────────

    async def create_charge(self, data: CreateChargeRequest) -> ChargeResponse:
        """
        Cria uma nova cobrança na API de pagamentos.

        Envia os dados via POST /charges e retorna a cobrança criada com
        todos os campos preenchidos pela API (id, status='pending', created_at).

        Args:
            data: Dados validados da cobrança — amount, currency e description
                  já passaram pela validação do Pydantic antes de chegarem aqui.

        Returns:
            ChargeResponse com os dados completos da cobrança recém-criada.

        Raises:
            PaymentClientError: Se a API retornar 4xx (ex: 422 payload inválido).
            PaymentUnavailableError: Se a API retornar 5xx após todos os retries.
            PaymentTimeoutError: Se o timeout esgotar em todas as tentativas.
            PaymentConnectionError: Se não for possível estabelecer conexão.
        """
        # model_dump(mode="json") serializa Decimal como string, garantindo
        # que o JSON enviado seja válido (Decimal não é serializável por padrão)
        response = await self._request(
            method="POST",
            path="/charges",
            json=data.model_dump(mode="json"),
        )
        return ChargeResponse.model_validate(response.json())

    async def get_charge(self, charge_id: str) -> ChargeResponse:
        """
        Consulta os dados atuais de uma cobrança existente.

        Faz GET /charges/{charge_id} e retorna o estado atual da cobrança.
        Útil para verificar se o pagamento foi confirmado após criação.

        Args:
            charge_id: Identificador único da cobrança (UUID gerado pela API).

        Returns:
            ChargeResponse com o estado atual da cobrança (status pode ter
            evoluído de 'pending' para 'paid', 'refunded' ou 'failed').

        Raises:
            PaymentNotFoundError: Se a cobrança não existir (404).
            PaymentClientError: Se a API retornar outro 4xx.
            PaymentUnavailableError: Se a API retornar 5xx após todos os retries.
            PaymentTimeoutError: Se o timeout esgotar em todas as tentativas.
            PaymentConnectionError: Se não for possível estabelecer conexão.
        """
        response = await self._request(
            method="GET",
            path=f"/charges/{charge_id}",
        )
        return ChargeResponse.model_validate(response.json())

    async def list_charges(self, page: int = 1, per_page: int = 10) -> ListChargesResponse:
        """
        Lista cobranças com paginação.

        Faz GET /charges?page=X&per_page=Y e retorna a página solicitada
        junto com metadados de paginação (total de registros, página atual).

        Args:
            page: Número da página (começa em 1). Default: 1.
            per_page: Quantidade de registros por página. Default: 10.

        Returns:
            ListChargesResponse com items (lista de ChargeResponse), total,
            page e per_page para permitir navegação paginada.

        Raises:
            PaymentClientError: Se a API retornar 4xx.
            PaymentUnavailableError: Se a API retornar 5xx após todos os retries.
            PaymentTimeoutError: Se o timeout esgotar em todas as tentativas.
            PaymentConnectionError: Se não for possível estabelecer conexão.
        """
        response = await self._request(
            method="GET",
            path="/charges",
            params={"page": page, "per_page": per_page},
        )
        return ListChargesResponse.model_validate(response.json())

    async def refund_charge(self, data: RefundRequest) -> RefundResponse:
        """
        Solicita reembolso de uma cobrança existente.

        Faz POST /charges/{charge_id}/refund. Se data.amount for None,
        a API processa reembolso total. Se informado, é um reembolso parcial.

        Args:
            data: Dados do reembolso — charge_id é obrigatório; amount é
                  opcional (None = reembolso total do valor da cobrança).

        Returns:
            RefundResponse com id, charge_id, amount reembolsado e status
            ('pending' ou 'processed' dependendo do processamento assíncrono).

        Raises:
            PaymentNotFoundError: Se a cobrança não existir (404).
            PaymentClientError: Se a cobrança já foi reembolsada (400) ou outro 4xx.
            PaymentUnavailableError: Se a API retornar 5xx após todos os retries.
            PaymentTimeoutError: Se o timeout esgotar em todas as tentativas.
            PaymentConnectionError: Se não for possível estabelecer conexão.
        """
        # exclude_none=True garante que amount não seja enviado quando None.
        # Se amount=None fosse serializado, a API receberia {"charge_id": "...", "amount": null}
        # e poderia rejeitar o request dependendo da validação do servidor.
        response = await self._request(
            method="POST",
            path=f"/charges/{data.charge_id}/refund",
            json=data.model_dump(mode="json", exclude_none=True),
        )
        return RefundResponse.model_validate(response.json())

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODO INTERNO DE RETRY
    # ─────────────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Executa uma requisição HTTP com retry automático e backoff exponencial.

        Este é o núcleo da resiliência do client. Toda chamada pública passa por
        aqui antes de chegar à API. A lógica implementa três pilares:

          1. RETRY para erros transitórios:
             - 5xx (500, 502, 503, 504…): servidor com falha temporária
             - 429 (Too Many Requests): rate limiting da API
             - httpx.TimeoutException: sem resposta no tempo configurado
             - httpx.ConnectError: falha de rede, DNS não resolvido, conexão recusada

          2. FALHA RÁPIDA para erros permanentes (sem retry):
             - 404: recurso não existe — retentar não vai criá-lo
             - Outros 4xx (exceto 429): erro no request — retentar enviaria o mesmo
               request inválido e geraria o mesmo erro

          3. BACKOFF EXPONENCIAL COM JITTER:
             - Fórmula: delay = backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
             - O componente 2^attempt aumenta o intervalo a cada tentativa:
                 attempt=0 → base * 1  (ex: 1.0s com base=1.0)
                 attempt=1 → base * 2  (ex: 2.0s com base=1.0)
                 attempt=2 → base * 4  (ex: 4.0s com base=1.0)
             - O jitter (random.uniform 0–0.5s) adiciona aleatoriedade para evitar
               que múltiplos clients sincronizem as retentativas e sobrecarreguem
               o servidor exatamente nos mesmos instantes (thundering herd problem)

        Args:
            method: Método HTTP em maiúsculas ('GET', 'POST', 'PUT', 'DELETE').
            path: Caminho relativo da API (ex: '/charges', '/charges/abc-123').
            **kwargs: Parâmetros extras repassados ao httpx.AsyncClient.request()
                      (json, params, headers, content, etc.).

        Returns:
            httpx.Response da primeira resposta bem-sucedida (2xx).

        Raises:
            PaymentNotFoundError: API retornou 404 (sem retry).
            PaymentClientError: API retornou 4xx ≠ 404 e ≠ 429 (sem retry).
            PaymentTimeoutError: Todas as tentativas esgotaram por timeout.
            PaymentConnectionError: Todas as tentativas falharam por erro de rede.
            PaymentUnavailableError: Todas as tentativas falharam com 5xx ou 429.
            RuntimeError: Se chamado fora de um bloco async with.
        """
        # Proteção contra uso sem context manager.
        # Sem o __aenter__, self._client é None e não há pool de conexões aberto.
        if self._client is None:
            raise RuntimeError(
                "PaymentClient deve ser usado como async context manager. "
                "Use: `async with PaymentClient(settings) as client:`"
            )

        # URL completa para logs (a base_url está no httpx.AsyncClient,
        # mas precisamos da URL completa para logar de forma legível)
        full_url = str(self._settings.payment_api_base_url).rstrip("/") + path

        # Rastreia o tipo e detalhes do último erro entre tentativas.
        # Necessário para determinar qual exceção levantar após esgotar o loop.
        # São resetados no início de cada iteração para refletir APENAS o último erro.
        last_exception: httpx.TimeoutException | httpx.ConnectError | None = None
        last_status_code: int | None = None
        last_response_body: str | None = None

        # ═════════════════════════════════════════════════════════════════════
        # LOOP DE RETRY
        #
        # Itera de attempt=0 até attempt=max_retries-1.
        # Em cada iteração:
        #   - Se o request tiver sucesso (2xx) → retorna imediatamente
        #   - Se for erro permanente (4xx exceto 429) → levanta exceção imediatamente
        #   - Se for erro transitório (5xx, 429, timeout, connect) → aguarda backoff
        #     e tenta novamente na próxima iteração
        #
        # Após o loop (se não retornou nem levantou dentro dele):
        #   → Todas as tentativas falharam → levanta exceção final
        # ═════════════════════════════════════════════════════════════════════
        for attempt in range(self._settings.payment_api_max_retries):

            # Tentativa em base 1 para logging (mais legível: "attempt 1 of 3")
            attempt_number = attempt + 1

            # Reseta o último erro no início de cada iteração.
            # Isso garante que, se esta tentativa falhar com 5xx mas a anterior
            # falhou com timeout, registramos corretamente o tipo da ÚLTIMA falha.
            last_exception = None

            # ── PASSO 1: Log do início da tentativa ───────────────────────────
            self._log.info(
                "http_request_start",
                method=method,
                url=full_url,
                attempt=attempt_number,
                max_retries=self._settings.payment_api_max_retries,
            )

            # ── PASSO 2: Marca o timestamp de início para medir duração ───────
            # time.perf_counter() usa o contador de alta resolução do SO,
            # mais preciso que time.time() para intervalos curtos (< 1 segundo).
            start_time = time.perf_counter()

            try:
                # ── PASSO 3: Executa o request HTTP ───────────────────────────
                # O httpx.AsyncClient usa a base_url configurada em __aenter__,
                # portanto `path` é relativo (ex: '/charges', não a URL completa).
                # kwargs pode conter json, params, headers, content, etc.
                response = await self._client.request(method, path, **kwargs)

                # ── PASSO 4: Calcula a duração em milissegundos ────────────────
                # Multiplicamos por 1000 para converter de segundos para ms.
                duration_ms = (time.perf_counter() - start_time) * 1000

                # ── PASSO 5: Log do response com status e duração ─────────────
                self._log.info(
                    "http_request_complete",
                    method=method,
                    url=full_url,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2),
                    attempt=attempt_number,
                )

                # ── PASSO 6: Sucesso (2xx) → retorna a response imediatamente ─
                # is_success verifica se status_code está no range 200–299.
                # Esta é a saída feliz — sem retry necessário.
                if response.is_success:
                    return response

                # ── PASSO 7: 404 → recurso não encontrado, falha sem retry ────
                # HTTP 404 significa que o recurso solicitado não existe na API.
                # Retentar o mesmo request não vai criar o recurso — é erro permanente.
                # PaymentNotFoundError herda de PaymentClientError, mas é mapeada
                # para HTTP 404 (não 400) no endpoint REST.
                if response.status_code == 404:
                    raise PaymentNotFoundError(
                        message=f"Recurso não encontrado: {method} {full_url}",
                        status_code=404,
                        # retries_attempted=attempt reflete quantas tentativas
                        # antecederam esta (sempre 0, pois 404 não dispara retry)
                        retries_attempted=attempt,
                        response_body=response.text,
                    )

                # ── PASSO 8: 4xx (exceto 429) → erro do cliente, sem retry ────
                # HTTP 4xx indica problema no request (payload inválido, token expirado,
                # recurso proibido, etc.). Retentar enviaria o mesmo request com o mesmo
                # erro — é uma falha permanente e não transitória.
                # EXCEÇÃO: 429 (Too Many Requests) é transitório (rate limit da API)
                # e deve ser retentado com backoff → tratado abaixo junto com 5xx.
                if response.is_client_error and response.status_code != 429:
                    raise PaymentClientError(
                        message=(
                            f"Erro do cliente HTTP {response.status_code}: "
                            f"{method} {full_url}"
                        ),
                        status_code=response.status_code,
                        retries_attempted=attempt,
                        response_body=response.text,
                    )

                # ── PASSO 9: 5xx ou 429 → erro transitório, vai retentar ───────
                # Registra os dados da resposta para usar na exceção final, caso
                # todas as tentativas se esgotem sem sucesso.
                last_status_code = response.status_code
                last_response_body = response.text

                self._log.warning(
                    "http_request_retriable_error",
                    method=method,
                    url=full_url,
                    status_code=response.status_code,
                    attempt=attempt_number,
                    max_retries=self._settings.payment_api_max_retries,
                    reason="5xx or 429 — will retry",
                )

            # ── PASSO 10: TimeoutException → sem resposta no tempo configurado ─
            # Captura a classe base httpx.TimeoutException, que inclui:
            #   - ReadTimeout: dados não chegaram no read_timeout configurado
            #   - ConnectTimeout: conexão TCP/TLS não estabelecida no connect_timeout
            #   - WriteTimeout: dados não foram enviados no write_timeout
            #   - PoolTimeout: nenhuma conexão disponível no pool dentro do timeout
            # Todos são transitórios — uma nova tentativa pode ter mais sorte.
            except httpx.TimeoutException as exc:
                duration_ms = (time.perf_counter() - start_time) * 1000
                last_exception = exc  # registra para determinar exceção final

                self._log.warning(
                    "http_request_timeout",
                    method=method,
                    url=full_url,
                    duration_ms=round(duration_ms, 2),
                    attempt=attempt_number,
                    max_retries=self._settings.payment_api_max_retries,
                    error=type(exc).__name__,
                )

            # ── PASSO 11: ConnectError → falha de rede ou DNS ─────────────────
            # httpx.ConnectError cobre:
            #   - DNS não resolvido (host não existe ou sem resposta DNS)
            #   - Conexão recusada (servidor não está escutando na porta)
            #   - Reset de conexão (servidor encerrou abruptamente)
            #   - Falhas de rede transitórias
            # Pode ser transitório em redes instáveis — vale retentar.
            except httpx.ConnectError as exc:
                duration_ms = (time.perf_counter() - start_time) * 1000
                last_exception = exc  # registra para determinar exceção final

                self._log.warning(
                    "http_request_connect_error",
                    method=method,
                    url=full_url,
                    duration_ms=round(duration_ms, 2),
                    error=str(exc),
                    attempt=attempt_number,
                    max_retries=self._settings.payment_api_max_retries,
                )

            # ── PASSOS 12–14: Backoff antes da próxima tentativa ──────────────
            # Só executa backoff se ainda houver tentativas restantes.
            # Na última iteração (attempt == max_retries - 1), pular o sleep
            # evita delay desnecessário antes de levantar a exceção final.
            is_last_attempt = attempt == self._settings.payment_api_max_retries - 1
            if not is_last_attempt:

                # ── PASSO 12: Calcula o delay com backoff exponencial + jitter ─
                #
                # FÓRMULA: delay = backoff_base * (2 ** attempt) + jitter
                #
                # Componente exponencial: backoff_base * 2^attempt
                #   attempt=0 → base * 1  (ex: 1.0s × 1 = 1.0s)
                #   attempt=1 → base * 2  (ex: 1.0s × 2 = 2.0s)
                #   attempt=2 → base * 4  (ex: 1.0s × 4 = 4.0s)
                #
                # Por que exponencial? A cada falha consecutiva, fica mais provável
                # que o servidor precise de mais tempo para se recuperar. Aumentar
                # o intervalo progressivamente respeita esse tempo de recuperação.
                #
                # Componente jitter: random.uniform(0, 0.5)
                #   Adiciona entre 0s e 0.5s aleatórios ao delay.
                #   Por que jitter? Se 100 clients falharam ao mesmo tempo (ex: restart
                #   do servidor), todos começariam a retentar no mesmo instante sem jitter,
                #   gerando uma nova sobrecarga (thundering herd). O jitter dispersa as
                #   tentativas aleatoriamente no tempo, suavizando a carga.
                jitter = random.uniform(0, 0.5)
                delay = self._settings.payment_api_backoff_base * (2**attempt) + jitter

                # ── PASSO 13: Log do delay antes da próxima tentativa ─────────
                self._log.info(
                    "http_request_retry_wait",
                    method=method,
                    url=full_url,
                    next_attempt=attempt_number + 1,
                    delay_seconds=round(delay, 3),
                )

                # ── PASSO 14: Aguarda o backoff (non-blocking) ─────────────────
                # asyncio.sleep() cede o controle ao event loop durante a espera,
                # permitindo que outros coroutines executem enquanto aguardamos.
                # Isso é crítico para não bloquear o servidor FastAPI durante o retry.
                await asyncio.sleep(delay)

        # ═════════════════════════════════════════════════════════════════════
        # ── PASSO 15: LOOP ESGOTADO — todas as tentativas falharam ───────────
        #
        # Se chegamos aqui, max_retries tentativas foram realizadas e nenhuma
        # foi bem-sucedida. Determinamos a exceção mais específica com base no
        # tipo do último erro registrado na última iteração do loop.
        #
        # Ordem de verificação:
        #   1. TimeoutException → PaymentTimeoutError (HTTP 504 no endpoint)
        #   2. ConnectError     → PaymentConnectionError (HTTP 502 no endpoint)
        #   3. Qualquer outro   → PaymentUnavailableError com último status (HTTP 502)
        # ═════════════════════════════════════════════════════════════════════
        total_attempts = self._settings.payment_api_max_retries

        # Verifica se o ÚLTIMO erro (da última iteração) foi timeout.
        # last_exception é resetado no início de cada iteração, então reflete
        # apenas o erro da tentativa mais recente — não de tentativas anteriores.
        if isinstance(last_exception, httpx.TimeoutException):
            self._log.error(
                "http_request_exhausted_timeout",
                method=method,
                url=full_url,
                total_attempts=total_attempts,
            )
            raise PaymentTimeoutError(
                message=(
                    f"Timeout após {total_attempts} tentativa(s): {method} {full_url}"
                ),
                retries_attempted=total_attempts,
            )

        # Verifica se o último erro foi falha de conexão (rede/DNS).
        if isinstance(last_exception, httpx.ConnectError):
            self._log.error(
                "http_request_exhausted_connect_error",
                method=method,
                url=full_url,
                total_attempts=total_attempts,
            )
            raise PaymentConnectionError(
                message=(
                    f"Falha de conexão após {total_attempts} tentativa(s): "
                    f"{method} {full_url}"
                ),
                retries_attempted=total_attempts,
            )

        # Se chegou aqui, o último erro foi um status 5xx ou 429 retornado pela API.
        # last_status_code e last_response_body foram preenchidos no passo 9.
        self._log.error(
            "http_request_exhausted_server_error",
            method=method,
            url=full_url,
            last_status_code=last_status_code,
            total_attempts=total_attempts,
        )
        raise PaymentUnavailableError(
            message=(
                f"API de pagamentos indisponível após {total_attempts} tentativa(s): "
                f"{method} {full_url} (último status HTTP: {last_status_code})"
            ),
            status_code=last_status_code,
            retries_attempted=total_attempts,
            response_body=last_response_body,
        )
