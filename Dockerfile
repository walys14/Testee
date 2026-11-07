# syntax=docker/dockerfile:1.7
#
# Estágios:
#   builder  -> instala dependências de runtime em /install (camada cacheável)
#   runtime  -> imagem enxuta, usuário não-root, só o necessário para executar
#   test     -> runtime + pytest/httpx + testes (usado pelo serviço "tests" do compose)
#   release  -> ÚLTIMO estágio = alvo padrão de `docker build .` (imagem de produção)

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# ---- dependências (só reinstala quando requirements.txt muda) ----------------
FROM base AS builder
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /install /usr/local
COPY src ./src
ENV PYTHONPATH=/app/src
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
# WEB_CONCURRENCY (nº de workers) é lido pelo uvicorn. Com o backend PostgreSQL,
# vários workers/réplicas são seguros: a exclusão mútua está no banco.
CMD ["uvicorn", "stock.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# ---- testes -----------------------------------------------------------------
FROM runtime AS test
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY pyproject.toml .
COPY tests ./tests
USER app
CMD ["pytest"]

# ---- release (padrão) -------------------------------------------------------
FROM runtime AS release
