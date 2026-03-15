"""
Módulo de configuração da aplicação.

Centraliza todas as variáveis de ambiente em uma classe Settings (pydantic-settings),
garantindo tipagem, validação e valores default para desenvolvimento local.
O .env é carregado automaticamente; quando ausente, os defaults permitem rodar sem setup.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Configurações da aplicação carregadas de variáveis de ambiente.

    Utiliza pydantic-settings para ler o arquivo .env (quando existir) e validar
    os tipos. Cada campo tem valor default compatível com o .env.example, de modo
    que a aplicação funcione sem .env em desenvolvimento. Os nomes dos campos em
    snake_case são mapeados automaticamente das variáveis em UPPER_SNAKE_CASE.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # --- API de Pagamentos ---
    payment_api_base_url: str = "http://localhost:8001"
    """URL base da API externa de pagamentos (ex.: mock server em desenvolvimento)."""

    payment_api_connect_timeout: float = 5.0
    """Timeout de conexão em segundos para a API de pagamentos."""

    payment_api_read_timeout: float = 30.0
    """Timeout de leitura da resposta em segundos."""

    payment_api_max_retries: int = 3
    """Número máximo de tentativas em caso de falha transitória (ex.: 5xx)."""

    payment_api_backoff_base: float = 1.0
    """Base em segundos para o cálculo do backoff exponencial entre retries."""

    # --- Logging ---
    log_level: str = "INFO"
    """Nível de log (ex.: DEBUG, INFO, WARNING, ERROR)."""

    log_format: str = "json"
    """Formato de saída dos logs: 'json' para produção, 'console' para desenvolvimento."""


@lru_cache
def get_settings() -> Settings:
    """
    Retorna uma instância cacheada de Settings.

    O lru_cache evita reler o .env e revalidar a cada chamada, o que é o padrão
    recomendado pelo FastAPI para injeção de dependência de configuração.
    A instância é criada uma única vez por processo.
    """
    return Settings()
