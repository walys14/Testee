"""Backend PostgreSQL: lock pessimista de linha (``SELECT ... FOR UPDATE``).

Por que isto resolve a concorrência
-----------------------------------
O estado de cada produto vive em UMA linha de ``products`` (contadores
``total/reserved/blocked``). ``SELECT ... FOR UPDATE`` serializa todas as
transações que tocam o mesmo produto, enquanto produtos diferentes seguem em
paralelo. Dentro da seção crítica o fluxo "ler → validar → gravar" é seguro.

Defesa em profundidade
----------------------
* ``CHECK (reserved + blocked <= total)`` no banco: mesmo um bug de código ou um
  acesso manual não consegue persistir estoque negativo.
* ``lock_timeout`` e ``statement_timeout`` por transação: sob contenção, a
  requisição falha rápido (``LockTimeoutError`` → HTTP 503) em vez de enfileirar
  para sempre e esgotar o pool.
* Pool com tamanho limitado e timeout de checkout: *backpressure* explícito.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool, PoolTimeout

from stock.domain.exceptions import (
    ConcurrencyConflictError,
    InvariantViolationError,
    LockTimeoutError,
    StorageUnavailableError,
)
from stock.domain.models import Hold, HoldKind, HoldStatus, Product

logger = logging.getLogger("stock.postgres")

_SCHEMA_ADVISORY_LOCK_KEY = 727_001  # serializa a criação do schema entre réplicas

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id         TEXT PRIMARY KEY,
    total      BIGINT NOT NULL DEFAULT 0,
    reserved   BIGINT NOT NULL DEFAULT 0,
    blocked    BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT products_non_negative CHECK (total >= 0 AND reserved >= 0 AND blocked >= 0),
    CONSTRAINT products_available_non_negative CHECK (reserved + blocked <= total)
);

CREATE TABLE IF NOT EXISTS holds (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL REFERENCES products (id),
    kind        TEXT NOT NULL CHECK (kind IN ('reservation', 'block')),
    quantity    BIGINT NOT NULL CHECK (quantity > 0),
    reference   TEXT,
    status      TEXT NOT NULL CHECK (status IN ('active', 'released')),
    created_at  TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    CONSTRAINT holds_release_consistency CHECK ((status = 'released') = (released_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS holds_product_status_idx ON holds (product_id, status);
"""


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Converte erros do driver em exceções do domínio (a ordem importa:
    ``LockNotAvailable``/``QueryCanceled`` são subclasses de ``OperationalError``)."""
    try:
        yield
    except pg_errors.LockNotAvailable as exc:
        raise LockTimeoutError("Timeout ao obter lock no banco de dados.") from exc
    except pg_errors.QueryCanceled as exc:
        raise LockTimeoutError("Operação excedeu o tempo limite (statement_timeout).") from exc
    except pg_errors.DeadlockDetected as exc:
        raise ConcurrencyConflictError("Deadlock detectado pelo banco.") from exc
    except pg_errors.CheckViolation as exc:
        # Defesa em profundidade acionada: o domínio deveria ter impedido isto.
        raise InvariantViolationError(
            "Constraint do banco violada.", constraint=exc.diag.constraint_name
        ) from exc
    except psycopg.OperationalError as exc:
        raise StorageUnavailableError("Banco de dados indisponível.") from exc


class PostgresBackend:
    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
        pool_timeout_s: float = 5.0,
        lock_timeout_ms: int = 2000,
        statement_timeout_ms: int = 5000,
        search_path: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if search_path:  # usado pelos testes para isolar cada execução em um schema próprio
            kwargs["options"] = f"-c search_path={search_path}"
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, timeout=pool_timeout_s, kwargs=kwargs, open=False
        )
        self._pool_timeout_s = pool_timeout_s
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms

    def open(self, wait_timeout_s: float = 30.0) -> None:
        try:
            self._pool.open(wait=True, timeout=wait_timeout_s)
        except PoolTimeout as exc:
            raise StorageUnavailableError("Não foi possível conectar ao banco de dados.") from exc

    def close(self) -> None:
        self._pool.close()

    def ensure_schema(self) -> None:
        """Criação idempotente (adequada ao case; em produção use migrações versionadas)."""
        with _translate_errors(), self._pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_ADVISORY_LOCK_KEY,))
            conn.execute(SCHEMA_SQL)

    def unit_of_work(self) -> PostgresUnitOfWork:
        return PostgresUnitOfWork(self)


class PostgresUnitOfWork:
    """Uma UoW = uma conexão = uma transação. Os locks de linha vivem até o fim dela."""

    def __init__(self, backend: PostgresBackend) -> None:
        self._backend = backend
        self._conn: psycopg.Connection | None = None
        self._committed = False
        self._locked: set[str] = set()
        self.products = _Products(self)
        self.holds = _Holds(self)

    def __enter__(self) -> PostgresUnitOfWork:
        b = self._backend
        try:
            self._conn = b._pool.getconn(timeout=b._pool_timeout_s)
        except PoolTimeout as exc:
            raise StorageUnavailableError("Pool de conexões esgotado.") from exc
        except psycopg.OperationalError as exc:
            raise StorageUnavailableError("Banco de dados indisponível.") from exc
        try:
            with _translate_errors():
                # Escopo de transação (3º argumento = true): vale só até o commit/rollback.
                self._conn.execute(
                    "SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                    (f"{b._lock_timeout_ms}ms", f"{b._statement_timeout_ms}ms"),
                )
        except BaseException:
            self._finish()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._finish()

    def commit(self) -> None:
        with _translate_errors():
            self._conn.commit()
        self._committed = True

    # -- interno ----------------------------------------------------------- #
    def _finish(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if not self._committed:
                conn.rollback()  # libera os locks de linha e descarta escritas
        except Exception:  # noqa: BLE001 - conexão possivelmente quebrada; o pool a descarta
            logger.warning("Falha ao dar rollback; conexão será descartada pelo pool.", exc_info=True)
        finally:
            self._backend._pool.putconn(conn)

    def _execute(self, sql: str, params: tuple = ()) -> psycopg.Cursor:
        with _translate_errors():
            return self._conn.execute(sql, params)

    def _require_lock(self, product_id: str) -> None:
        if product_id not in self._locked:
            raise InvariantViolationError("Escrita sem lock do produto.", product_id=product_id)


class _Products:
    def __init__(self, uow: PostgresUnitOfWork) -> None:
        self._uow = uow

    def get(self, product_id: str) -> Product | None:
        row = self._uow._execute(
            "SELECT total, reserved, blocked FROM products WHERE id = %s", (product_id,)
        ).fetchone()
        return None if row is None else Product.restore(product_id, *row)

    def get_for_update(self, product_id: str) -> Product | None:
        row = self._uow._execute(
            "SELECT total, reserved, blocked FROM products WHERE id = %s FOR UPDATE", (product_id,)
        ).fetchone()
        if row is None:
            return None
        self._uow._locked.add(product_id)
        return Product.restore(product_id, *row)

    def get_or_create_for_update(self, product_id: str) -> Product:
        # INSERT ... ON CONFLICT DO NOTHING é atômico: duas "primeiras entradas"
        # concorrentes resultam em UMA linha; a segunda espera a primeira commitar.
        self._uow._execute(
            "INSERT INTO products (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (product_id,)
        )
        product = self.get_for_update(product_id)
        if product is None:  # impossível: acabamos de inserir/encontrar a linha
            raise InvariantViolationError("Produto não encontrado após upsert.", product_id=product_id)
        return product

    def save(self, product: Product) -> None:
        self._uow._require_lock(product.id)
        self._uow._execute(
            "UPDATE products SET total = %s, reserved = %s, blocked = %s, updated_at = now() WHERE id = %s",
            (product.total, product.reserved, product.blocked, product.id),
        )


class _Holds:
    _COLUMNS = "id, product_id, kind, quantity, reference, status, created_at, released_at"

    def __init__(self, uow: PostgresUnitOfWork) -> None:
        self._uow = uow

    def find_product_id(self, hold_id: str) -> str | None:
        row = self._uow._execute("SELECT product_id FROM holds WHERE id = %s", (hold_id,)).fetchone()
        return None if row is None else row[0]

    def get(self, hold_id: str) -> Hold | None:
        row = self._uow._execute(f"SELECT {self._COLUMNS} FROM holds WHERE id = %s", (hold_id,)).fetchone()
        return None if row is None else self._to_hold(row)

    def list_by_product(self, product_id: str, *, only_active: bool) -> list[Hold]:
        # Usa o índice (product_id, status); limite defensivo para não devolver
        # uma lista ilimitada a um produto com muitos holds.
        sql = f"SELECT {self._COLUMNS} FROM holds WHERE product_id = %s"
        params: tuple = (product_id,)
        if only_active:
            sql += " AND status = 'active'"
        sql += " ORDER BY created_at DESC, id DESC LIMIT 200"
        rows = self._uow._execute(sql, params).fetchall()
        return [self._to_hold(row) for row in rows]

    @staticmethod
    def _to_hold(row: tuple) -> Hold:
        id_, product_id, kind, quantity, reference, status, created_at, released_at = row
        return Hold(id_, product_id, HoldKind(kind), quantity, reference, HoldStatus(status), created_at, released_at)

    def save(self, hold: Hold) -> None:
        self._uow._require_lock(hold.product_id)
        self._uow._execute(
            f"""
            INSERT INTO holds ({self._COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, released_at = EXCLUDED.released_at
            """,
            (hold.id, hold.product_id, hold.kind.value, hold.quantity, hold.reference,
             hold.status.value, hold.created_at, hold.released_at),
        )
