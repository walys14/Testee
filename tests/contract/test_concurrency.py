"""Testes de concorrência: o coração da garantia de consistência.

Cada teste dispara muitas threads simultâneas contra o mesmo produto e verifica
*contabilidade exata* no final — não apenas "não deu erro". Se o lock pessimista
for removido, estes testes falham (estouro de estoque / liberação em dobro).

Rodam contra todos os backends (memória e PostgreSQL).
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from stock.application.service import StockService
from stock.domain.exceptions import (
    HoldAlreadyReleasedError,
    InsufficientStockError,
    LockTimeoutError,
)
from stock.domain.models import HoldKind, HoldStatus
from tests.conftest import BACKEND_KINDS, open_backend

WORKERS = 32


def _slow_clock() -> datetime:
    """Relógio injetado que dorme 2 ms.

    O serviço o chama *dentro* da seção crítica, depois de validar e antes de
    commitar. Isso alarga a janela da corrida (check-then-act) sem tocar no código
    de produção: sem o lock, as threads se atropelam de forma quase determinística.
    Com o lock, só custa 2 ms por operação serializada do mesmo produto.
    """
    time.sleep(0.002)
    return datetime.now(timezone.utc)


@pytest.fixture
def racy_service(backend) -> StockService:
    return StockService(backend.unit_of_work, clock=_slow_clock)


def run_concurrently(tasks):
    """Executa as tarefas liberando todas ao mesmo tempo (maximiza a disputa).

    Exceções inesperadas propagam via ``future.result()`` e falham o teste.
    """
    start = threading.Event()

    def wrapped(task):
        start.wait(timeout=10)
        return task()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(wrapped, t) for t in tasks]
        time.sleep(0.1)  # deixa as threads chegarem ao portão antes de liberar
        start.set()
        return [f.result(timeout=60) for f in futures]


def attempt(call):
    try:
        call()
        return "ok"
    except InsufficientStockError:
        return "insufficient"
    except HoldAlreadyReleasedError:
        return "already_released"


# --------------------------------------------------------------------------- #
def test_concurrent_reservations_never_oversell(racy_service):
    racy_service.add_stock("sku", 100)

    results = run_concurrently([lambda i=i: attempt(lambda: racy_service.reserve("sku", 1, f"order-{i}")) for i in range(300)])

    assert results.count("ok") == 100
    assert results.count("insufficient") == 200
    snap = racy_service.get_stock("sku")
    assert (snap.total, snap.available, snap.reserved, snap.blocked) == (100, 0, 100, 0)


def test_concurrent_reservations_of_uneven_size_respect_the_limit(racy_service):
    racy_service.add_stock("sku", 1000)

    results = run_concurrently([lambda i=i: attempt(lambda: racy_service.reserve("sku", 7, f"o-{i}")) for i in range(200)])

    assert results.count("ok") == 1000 // 7  # 142
    snap = racy_service.get_stock("sku")
    assert snap.reserved == 142 * 7 and snap.available == 1000 - 142 * 7 and snap.available < 7


def test_race_between_withdrawals_reservations_and_blocks_never_oversells(racy_service):
    racy_service.add_stock("sku", 30)
    tasks = (
        [lambda i=i: ("remove", attempt(lambda: racy_service.remove_stock("sku", 1))) for i in range(30)]
        + [lambda i=i: ("reserve", attempt(lambda: racy_service.reserve("sku", 1, f"o-{i}"))) for i in range(30)]
        + [lambda: ("block", attempt(lambda: racy_service.block("sku", 1))) for _ in range(30)]
    )
    results = run_concurrently(tasks)

    ok = {op: sum(1 for o, r in results if o == op and r == "ok") for op in ("remove", "reserve", "block")}
    assert sum(ok.values()) == 30  # exatamente as 30 unidades, nem mais nem menos
    snap = racy_service.get_stock("sku")
    assert snap.total == 30 - ok["remove"]
    assert snap.reserved == ok["reserve"] and snap.blocked == ok["block"]
    assert snap.available == 0


def test_concurrent_releases_of_the_same_hold_credit_only_once(racy_service):
    racy_service.add_stock("sku", 10)
    hold = racy_service.reserve("sku", 10, "order-1")

    results = run_concurrently([lambda: attempt(lambda: racy_service.release_reservation(hold.id)) for _ in range(20)])

    assert results.count("ok") == 1
    assert results.count("already_released") == 19
    snap = racy_service.get_stock("sku")
    assert (snap.total, snap.available, snap.reserved) == (10, 10, 0)  # devolvido UMA vez


def test_concurrent_first_entries_create_a_single_product_and_sum_correctly(service):
    run_concurrently([lambda: service.add_stock("brand-new", 10) for _ in range(30)])
    assert service.get_stock("brand-new").total == 300


def test_mixed_random_workload_keeps_exact_accounting(racy_service):
    racy_service.add_stock("sku", 500)

    def worker(seed: int):
        rng = random.Random(seed)
        added = removed = 0
        mine = []  # holds ativos criados por ESTA thread (ninguém mais os libera)
        for _ in range(60):
            op, qty = rng.choice(["add", "remove", "reserve", "block", "release"]), rng.randint(1, 20)
            try:
                if op == "add":
                    racy_service.add_stock("sku", qty)
                    added += qty
                elif op == "remove":
                    racy_service.remove_stock("sku", qty)
                    removed += qty
                elif op == "reserve":
                    mine.append(racy_service.reserve("sku", qty, f"req-{seed}"))
                elif op == "block":
                    mine.append(racy_service.block("sku", qty, f"why-{seed}"))
                elif mine:
                    hold = mine.pop(rng.randrange(len(mine)))
                    (racy_service.release_reservation if hold.kind is HoldKind.RESERVATION else racy_service.release_block)(hold.id)
            except InsufficientStockError:
                pass
        return added, removed, mine

    outcomes = run_concurrently([lambda s=s: worker(s) for s in range(16)])

    added = sum(o[0] for o in outcomes)
    removed = sum(o[1] for o in outcomes)
    active = [h for o in outcomes for h in o[2]]
    expected_reserved = sum(h.quantity for h in active if h.kind is HoldKind.RESERVATION)
    expected_blocked = sum(h.quantity for h in active if h.kind is HoldKind.BLOCK)

    snap = racy_service.get_stock("sku")
    assert snap.total == 500 + added - removed
    assert snap.reserved == expected_reserved
    assert snap.blocked == expected_blocked
    assert snap.available == snap.total - snap.reserved - snap.blocked >= 0
    for hold in active:  # o que o sistema diz bate com o que cada thread acredita ter
        stored = racy_service.get_reservation(hold.id) if hold.kind is HoldKind.RESERVATION else racy_service.get_block(hold.id)
        assert stored.status is HoldStatus.ACTIVE and stored.quantity == hold.quantity


# --------------------------------------------------------------------------- #
# Fail-fast sob contenção
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", BACKEND_KINDS)
def test_lock_contention_fails_fast_and_other_products_are_not_blocked(kind):
    with open_backend(kind, lock_timeout_ms=300) as backend:
        service = StockService(backend.unit_of_work)
        service.add_stock("A", 10)
        service.add_stock("B", 10)

        with backend.unit_of_work() as holder:
            holder.products.get_for_update("A")  # segura o lock do produto A

            started = time.monotonic()
            with pytest.raises(LockTimeoutError) as err:
                service.reserve("A", 1, "order-1")
            assert time.monotonic() - started < 3.0  # falhou rápido, não ficou preso
            assert err.value.retryable

            service.reserve("B", 1, "order-2")  # produto diferente: sem contenção

        service.reserve("A", 1, "order-3")  # lock liberado: volta a funcionar
        snap = service.get_stock("A")
        assert (snap.available, snap.reserved) == (9, 1)  # a tentativa que deu timeout não deixou rastro
