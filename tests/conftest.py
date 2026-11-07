"""Fixtures compartilhadas.

A mesma suíte de *contrato* roda contra dois backends (memória e PostgreSQL). O
Postgres só entra quando ``DATABASE_URL`` está definida; caso contrário os
testes marcados como ``integration`` são pulados (não falham).

Isolamento: cada teste PostgreSQL cria um *schema* próprio (``test_<hex>``) e o
remove no final. Assim a suíte pode rodar no mesmo servidor/banco da aplicação
sem tocar nos dados dela, e testes não interferem entre si.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from stock.application.service import StockService
from stock.infrastructure.memory import InMemoryBackend

DATABASE_URL = os.getenv("DATABASE_URL")

BACKEND_KINDS = [
    pytest.param("memory", id="memory"),
    pytest.param("postgres", id="postgres", marks=pytest.mark.integration),
]


@contextmanager
def open_backend(kind: str, *, lock_timeout_ms: int = 2000, pool_max: int = 20) -> Iterator[object]:
    if kind == "memory":
        yield InMemoryBackend(lock_timeout_s=lock_timeout_ms / 1000)
        return

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL não definida: teste de integração com PostgreSQL ignorado.")

    import psycopg

    from stock.infrastructure.postgres import PostgresBackend

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    # Pool aquecido (min_size == max_size): com poucas conexões abertas as threads se
    # serializariam esperando conexão e a corrida simplesmente não aconteceria,
    # mascarando a ausência de lock (descoberto via teste de mutação).
    backend = PostgresBackend(
        DATABASE_URL, min_size=pool_max, max_size=pool_max, lock_timeout_ms=lock_timeout_ms, search_path=schema
    )
    backend.open()
    backend.ensure_schema()
    try:
        yield backend
    finally:
        backend.close()
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=BACKEND_KINDS)
def backend_kind(request) -> str:
    return request.param


@pytest.fixture
def backend(backend_kind):
    with open_backend(backend_kind) as b:
        yield b


@pytest.fixture
def service(backend) -> StockService:
    return StockService(backend.unit_of_work)
