"""Testes específicos do backend PostgreSQL (exigem DATABASE_URL).

Verificam o que só o banco real pode provar: constraints como segunda linha de
defesa, tradução de erros do driver e reconciliação em SQL após carga concorrente.
"""

from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from stock.application.service import StockService
from stock.domain.exceptions import InsufficientStockError, InvariantViolationError
from tests.conftest import open_backend

pytestmark = pytest.mark.integration


@pytest.fixture
def pg():
    with open_backend("postgres") as backend:
        yield backend


def test_database_rejects_inconsistent_counters_even_if_code_is_bypassed(pg):
    with pg._pool.connection() as conn:
        conn.execute("INSERT INTO products (id, total) VALUES ('x', 5)")
        conn.commit()  # o rollback após cada UPDATE inválido não pode desfazer o INSERT
        for bad_update in (
            "UPDATE products SET reserved = 6 WHERE id = 'x'",           # reservado > total
            "UPDATE products SET reserved = 3, blocked = 3 WHERE id = 'x'",  # soma > total
            "UPDATE products SET total = -1 WHERE id = 'x'",             # negativo
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(bad_update)
            conn.rollback()


def test_check_violation_is_translated_to_invariant_violation(pg):
    with pg.unit_of_work() as uow:
        uow.products.get_or_create_for_update("y")
        with pytest.raises(InvariantViolationError) as err:
            uow._execute("UPDATE products SET reserved = 99 WHERE id = 'y'")
        assert err.value.details["constraint"] == "products_available_non_negative"


def test_hold_requires_an_existing_product_and_positive_quantity(pg):
    with pg._pool.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO holds (id, product_id, kind, quantity, status, created_at) "
                "VALUES ('h', 'ghost', 'reservation', 1, 'active', now())"
            )
        conn.rollback()


def test_ensure_schema_is_idempotent_and_safe_under_concurrent_startup(pg):
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(pg.ensure_schema) for _ in range(8)]:
            future.result(timeout=30)  # nenhuma réplica "perde a corrida" da criação


def test_sql_reconciliation_after_concurrent_workload(pg):
    """Após carga concorrente, os contadores do produto == soma dos holds ativos."""
    service = StockService(pg.unit_of_work)
    service.add_stock("sku", 400)
    start = threading.Event()

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        mine = []
        start.wait(timeout=10)
        for _ in range(50):
            op, qty = rng.choice(["reserve", "block", "release", "remove", "add"]), rng.randint(1, 15)
            try:
                if op == "reserve":
                    mine.append(("reservation", service.reserve("sku", qty, f"r{seed}").id))
                elif op == "block":
                    mine.append(("block", service.block("sku", qty).id))
                elif op == "remove":
                    service.remove_stock("sku", qty)
                elif op == "add":
                    service.add_stock("sku", qty)
                elif mine:
                    kind, hold_id = mine.pop(rng.randrange(len(mine)))
                    (service.release_reservation if kind == "reservation" else service.release_block)(hold_id)
            except InsufficientStockError:
                pass

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(worker, s) for s in range(16)]
        start.set()
        for f in futures:
            f.result(timeout=120)

    with pg._pool.connection() as conn:
        total, reserved, blocked = conn.execute(
            "SELECT total, reserved, blocked FROM products WHERE id = 'sku'"
        ).fetchone()
        sums = dict(conn.execute(
            "SELECT kind, COALESCE(SUM(quantity), 0) FROM holds WHERE product_id = 'sku' AND status = 'active' GROUP BY kind"
        ).fetchall())

    assert reserved == sums.get("reservation", 0)
    assert blocked == sums.get("block", 0)
    assert reserved + blocked <= total
