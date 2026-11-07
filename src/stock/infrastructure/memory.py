"""Backend em memória.

Serve a três propósitos: (1) execução local sem dependências, (2) *test double*
rápido e (3) implementação de referência do contrato das portas — a mesma suíte
de contrato roda contra ele e contra o PostgreSQL.

⚠ Só é correto dentro de UM processo: o lock é um ``threading.Lock``. Em
produção com várias instâncias use o backend PostgreSQL (lock de linha no banco).

Mecânica
--------
* Lock pessimista = um ``threading.Lock`` por produto, adquirido em
  ``get_for_update`` e mantido até a UoW sair (``__exit__``).
* Aquisição com *timeout* → ``LockTimeoutError`` (fail-fast, em vez de fila infinita).
* Escritas ficam em buffers (``pending``) e só são aplicadas no ``commit``, sob
  um mutex curto → atomicidade e "rollback" gratuito (basta descartar o buffer).
"""

from __future__ import annotations

import threading

from stock.domain.exceptions import InvariantViolationError, LockTimeoutError
from stock.domain.models import Hold, Product


class InMemoryBackend:
    def __init__(self, lock_timeout_s: float = 5.0) -> None:
        self._lock_timeout_s = lock_timeout_s
        self._state_lock = threading.Lock()  # protege os dicts abaixo (seções curtíssimas)
        self._products: dict[str, tuple[int, int, int]] = {}  # id -> (total, reserved, blocked)
        self._holds: dict[str, Hold] = {}
        self._product_locks: dict[str, threading.Lock] = {}

    def unit_of_work(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)

    def close(self) -> None:  # simetria com o backend Postgres
        pass

    # -- usados pela UoW -------------------------------------------------- #
    def _lock_for(self, product_id: str) -> threading.Lock:
        with self._state_lock:
            return self._product_locks.setdefault(product_id, threading.Lock())

    def _read_product(self, product_id: str) -> Product | None:
        with self._state_lock:
            row = self._products.get(product_id)
        return None if row is None else Product.restore(product_id, *row)


class InMemoryUnitOfWork:
    def __init__(self, backend: InMemoryBackend) -> None:
        self._backend = backend
        self._held: dict[str, threading.Lock] = {}
        self._pending_products: dict[str, tuple[int, int, int]] = {}
        self._pending_holds: dict[str, Hold] = {}
        self._committed = False
        self.products = _Products(self)
        self.holds = _Holds(self)

    def __enter__(self) -> InMemoryUnitOfWork:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for lock in self._held.values():
            lock.release()
        self._held.clear()
        self._pending_products.clear()
        self._pending_holds.clear()

    def commit(self) -> None:
        b = self._backend
        with b._state_lock:  # aplica produto + holds de forma indivisível para leitores
            b._products.update(self._pending_products)
            b._holds.update(self._pending_holds)
        self._pending_products.clear()
        self._pending_holds.clear()
        self._committed = True

    # -- interno ----------------------------------------------------------- #
    def _acquire(self, product_id: str) -> None:
        if product_id in self._held:  # reentrante dentro da mesma UoW
            return
        lock = self._backend._lock_for(product_id)
        if not lock.acquire(timeout=self._backend._lock_timeout_s):
            raise LockTimeoutError(
                f"Timeout ao obter o lock do produto '{product_id}'.",
                product_id=product_id, timeout_s=self._backend._lock_timeout_s,
            )
        self._held[product_id] = lock

    def _require_lock(self, product_id: str) -> None:
        if product_id not in self._held:
            raise InvariantViolationError("Escrita sem lock do produto.", product_id=product_id)


class _Products:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow

    def get(self, product_id: str) -> Product | None:
        return self._uow._backend._read_product(product_id)

    def get_for_update(self, product_id: str) -> Product | None:
        # Existência só vai de "não" para "sim": um "não" pode ser devolvido sem
        # travar, o que evita criar locks para ids arbitrários/inexistentes.
        if self._uow._backend._read_product(product_id) is None:
            return None
        self._uow._acquire(product_id)
        return self._uow._backend._read_product(product_id)  # re-lê já sob lock

    def get_or_create_for_update(self, product_id: str) -> Product:
        self._uow._acquire(product_id)
        return self._uow._backend._read_product(product_id) or Product(product_id)

    def save(self, product: Product) -> None:
        self._uow._require_lock(product.id)
        self._uow._pending_products[product.id] = (product.total, product.reserved, product.blocked)


class _Holds:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow

    def find_product_id(self, hold_id: str) -> str | None:
        hold = self.get(hold_id)
        return None if hold is None else hold.product_id

    def get(self, hold_id: str) -> Hold | None:
        pending = self._uow._pending_holds.get(hold_id)
        if pending is not None:
            return pending
        with self._uow._backend._state_lock:
            return self._uow._backend._holds.get(hold_id)

    def save(self, hold: Hold) -> None:
        self._uow._require_lock(hold.product_id)
        self._uow._pending_holds[hold.id] = hold
