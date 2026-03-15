# Dockerfile — Imagem Docker da aplicação principal (FastAPI + PaymentClient).
#
# Usa multi-stage implícito com uv copiado da imagem oficial para instalar
# dependências de forma reproduzível (--frozen respeita o uv.lock).
# A imagem base slim reduz o tamanho final ao mínimo necessário.

FROM python:3.12-slim

WORKDIR /app

# Copia o binário do uv diretamente da imagem oficial,
# evitando instalar via pip e mantendo a versão controlada.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copia apenas os arquivos de dependência primeiro para aproveitar
# o cache de camadas do Docker: se pyproject.toml e uv.lock não mudaram,
# a instalação de deps não precisa rodar novamente no próximo build.
COPY pyproject.toml uv.lock ./

# Instala somente dependências de produção (--no-dev),
# usando exatamente as versões fixadas no lockfile (--frozen).
RUN uv sync --frozen --no-dev

# Copia o código da aplicação após instalar deps (melhor aproveitamento de cache).
COPY app/ app/

# Copia o .env.example como .env padrão dentro do container.
# Em produção real, variáveis devem vir do orchestrador via environment no compose.
COPY .env.example .env

EXPOSE 8000

# Invoca o uvicorn diretamente pelo venv criado pelo uv (em vez de `uv run`).
# Motivo: `uv run` re-sincroniza o ambiente em cada execução, o que instalaria
# as dev-dependencies do lockfile em runtime, atrasando o startup em ~30s.
# Usando o binário do venv diretamente, o container sobe instantaneamente.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
