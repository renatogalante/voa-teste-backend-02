"""
Hierarquia de exceções tipadas para o client de pagamentos.

Este módulo define exceções customizadas que representam os diferentes tipos
de falha que podem ocorrer ao interagir com a API externa de pagamentos.
Cada exceção carrega informações contextuais (status HTTP, tentativas de retry,
corpo da resposta) que permitem ao endpoint REST mapear o erro para o status
HTTP adequado na resposta ao consumidor.

Mapeamento para HTTP status codes no endpoint:
    - PaymentTimeoutError      → 504 Gateway Timeout
    - PaymentUnavailableError  → 502 Bad Gateway
    - PaymentClientError       → 400 Bad Request
    - PaymentNotFoundError     → 404 Not Found
    - PaymentConnectionError   → 502 Bad Gateway

Hierarquia:
    PaymentServiceError (base)
    ├── PaymentTimeoutError
    ├── PaymentUnavailableError
    ├── PaymentClientError
    │   └── PaymentNotFoundError
    └── PaymentConnectionError
"""


class PaymentServiceError(Exception):
    """
    Exceção base para todos os erros do serviço de pagamentos.

    Centraliza os atributos comuns a todas as falhas do client HTTP:
    mensagem descritiva, código de status HTTP (quando disponível) e
    quantidade de tentativas realizadas antes de desistir.

    Subclasses especializam o contexto (timeout, indisponibilidade,
    erro do chamador, falha de rede) sem repetir a estrutura base.

    Attributes:
        message: Descrição legível do erro ocorrido.
        status_code: Código HTTP retornado pela API, ou None quando
                     não houve resposta (timeout, erro de conexão).
        retries_attempted: Número de tentativas realizadas antes de
                           levantar a exceção.
    """

    def __init__(
        self,
        message: str = "Erro no serviço de pagamentos",
        status_code: int | None = None,
        retries_attempted: int = 0,
    ) -> None:
        """
        Inicializa a exceção base com informações contextuais.

        Args:
            message: Descrição legível do erro.
            status_code: Código HTTP da última resposta, ou None se
                         não houve resposta.
            retries_attempted: Quantas tentativas foram feitas.
        """
        self.message: str = message
        self.status_code: int | None = status_code
        self.retries_attempted: int = retries_attempted
        super().__init__(message)

    def __str__(self) -> str:
        """
        Retorna representação descritiva incluindo status e tentativas.

        Formato:
            [PaymentServiceError] Mensagem (status_code=503, retries=3)
            [PaymentServiceError] Mensagem (status_code=N/A, retries=0)

        Returns:
            String formatada com o nome da classe, mensagem, status e retries.
        """
        status_display: str = str(self.status_code) if self.status_code is not None else "N/A"
        return (
            f"[{self.__class__.__name__}] {self.message} "
            f"(status_code={status_display}, retries={self.retries_attempted})"
        )


class PaymentTimeoutError(PaymentServiceError):
    """
    O client não recebeu resposta dentro do tempo configurado.

    Levantada quando todas as tentativas de retry se esgotaram por timeout
    (httpx.TimeoutException). Como não houve resposta HTTP, status_code
    é sempre None.

    No endpoint REST, é mapeada para HTTP 504 Gateway Timeout.

    Attributes:
        message: Descrição do erro de timeout.
        status_code: Sempre None (não houve resposta).
        retries_attempted: Número de tentativas realizadas.
    """

    def __init__(
        self,
        message: str = "Timeout ao aguardar resposta da API de pagamentos",
        retries_attempted: int = 0,
    ) -> None:
        """
        Inicializa exceção de timeout.

        O status_code é fixado em None porque timeouts não produzem
        resposta HTTP.

        Args:
            message: Descrição do erro de timeout.
            retries_attempted: Quantas tentativas foram feitas antes
                               de desistir.
        """
        super().__init__(
            message=message,
            status_code=None,
            retries_attempted=retries_attempted,
        )


class PaymentUnavailableError(PaymentServiceError):
    """
    A API de pagamentos retornou erro 5xx em todas as tentativas.

    Levantada quando o retry se esgota e a última resposta foi um erro
    do servidor (HTTP 500, 502, 503, etc.). Carrega o status_code e o
    corpo da última resposta para diagnóstico.

    No endpoint REST, é mapeada para HTTP 502 Bad Gateway.

    Attributes:
        message: Descrição do erro de indisponibilidade.
        status_code: Código HTTP 5xx da última resposta.
        retries_attempted: Número de tentativas realizadas.
        response_body: Corpo da última resposta HTTP, quando disponível.
    """

    def __init__(
        self,
        message: str = "API de pagamentos indisponível após múltiplas tentativas",
        status_code: int | None = None,
        retries_attempted: int = 0,
        response_body: str | None = None,
    ) -> None:
        """
        Inicializa exceção de indisponibilidade.

        Args:
            message: Descrição do erro.
            status_code: Código HTTP 5xx da última resposta.
            retries_attempted: Quantas tentativas foram feitas.
            response_body: Corpo da última resposta, útil para debug.
        """
        self.response_body: str | None = response_body
        super().__init__(
            message=message,
            status_code=status_code,
            retries_attempted=retries_attempted,
        )


class PaymentClientError(PaymentServiceError):
    """
    A API retornou erro 4xx — indica erro do chamador.

    Levantada imediatamente ao receber HTTP 4xx, SEM disparar retry,
    pois erros do cliente (payload inválido, recurso não encontrado, etc.)
    não se resolvem com novas tentativas.

    No endpoint REST, é mapeada para HTTP 400 Bad Request.

    Attributes:
        message: Descrição do erro do cliente.
        status_code: Código HTTP 4xx retornado pela API.
        retries_attempted: Sempre 0 (retry não é tentado para 4xx).
        response_body: Corpo da resposta com detalhes do erro.
    """

    def __init__(
        self,
        message: str = "Erro do cliente na API de pagamentos",
        status_code: int | None = None,
        retries_attempted: int = 0,
        response_body: str | None = None,
    ) -> None:
        """
        Inicializa exceção de erro do cliente.

        Args:
            message: Descrição do erro.
            status_code: Código HTTP 4xx da resposta.
            retries_attempted: Número de tentativas (tipicamente 0).
            response_body: Corpo da resposta com detalhes do erro.
        """
        self.response_body: str | None = response_body
        super().__init__(
            message=message,
            status_code=status_code,
            retries_attempted=retries_attempted,
        )


class PaymentNotFoundError(PaymentClientError):
    """
    A API retornou HTTP 404 — recurso não encontrado.

    Levantada quando a API indica que o recurso solicitado não existe
    (ex: cobrança com ID inexistente). Herda de PaymentClientError para
    manter compatibilidade — quem captura PaymentClientError também
    captura esta exceção automaticamente.

    No endpoint REST, é mapeada para HTTP 404 Not Found (em vez do
    400 genérico de PaymentClientError).

    Attributes:
        message: Descrição do erro de recurso não encontrado.
        status_code: Código HTTP 404 retornado pela API.
        retries_attempted: Sempre 0 (retry não é tentado para 4xx).
        response_body: Corpo da resposta com detalhes do erro.
    """

    def __init__(
        self,
        message: str = "Recurso não encontrado na API de pagamentos",
        status_code: int | None = 404,
        retries_attempted: int = 0,
        response_body: str | None = None,
    ) -> None:
        """
        Inicializa exceção de recurso não encontrado.

        O status_code tem default 404, mas pode ser sobrescrito caso
        a API use outro código para indicar "não encontrado".

        Args:
            message: Descrição do erro.
            status_code: Código HTTP da resposta (default: 404).
            retries_attempted: Número de tentativas (tipicamente 0).
            response_body: Corpo da resposta com detalhes do erro.
        """
        super().__init__(
            message=message,
            status_code=status_code,
            retries_attempted=retries_attempted,
            response_body=response_body,
        )


class PaymentConnectionError(PaymentServiceError):
    """
    Falha de rede ao tentar alcançar a API de pagamentos.

    Levantada quando ocorre erro de conexão (DNS não resolvido, conexão
    recusada, reset de conexão, etc.). Como não houve resposta HTTP,
    status_code é sempre None.

    No endpoint REST, é mapeada para HTTP 502 Bad Gateway.

    Attributes:
        message: Descrição do erro de conexão.
        status_code: Sempre None (não houve resposta).
        retries_attempted: Número de tentativas realizadas.
    """

    def __init__(
        self,
        message: str = "Falha de conexão com a API de pagamentos",
        retries_attempted: int = 0,
    ) -> None:
        """
        Inicializa exceção de erro de conexão.

        O status_code é fixado em None porque falhas de rede não
        produzem resposta HTTP.

        Args:
            message: Descrição do erro de conexão.
            retries_attempted: Quantas tentativas foram feitas antes
                               de desistir.
        """
        super().__init__(
            message=message,
            status_code=None,
            retries_attempted=retries_attempted,
        )
