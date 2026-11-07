"""Casos de uso do controle de estoque.

Todo comando segue o mesmo molde (template de seção crítica):

    1. validar a entrada            (fail-fast, FORA do lock: lock curto)
    2. abrir UnitOfWork
    3. carregar o produto SOB LOCK  (lock pessimista, 1 produto por operação)
    4. executar o comando no domínio (check-then-act)
    5. persistir e commitar
    6. sair da UoW                  (libera o lock; rollback se algo falhou)

Como cada operação trava exatamente UM produto, não há ordem de aquisição de
locks a respeitar e, portanto, não há deadlock entre operações deste serviço.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from stock.domain.exceptions import (
    HoldNotFoundError,
    InvariantViolationError,
    ProductNotFoundError,
)
from stock.domain.models import (
    Hold,
    HoldKind,
    Product,
    StockSnapshot,
    validate_identifier,
    validate_quantity,
)

from .ports import UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger("stock.service")


class StockService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)

    # ------------------------------------------------------------------ #
    # Comandos
    # ------------------------------------------------------------------ #
    def add_stock(self, product_id: str, quantity: int) -> StockSnapshot:
        """Entrada. Cria o produto se for a primeira entrada (premissa documentada)."""
        pid = validate_identifier(product_id, "product_id")
        qty = validate_quantity(quantity)
        with self._uow_factory() as uow:
            product = uow.products.get_or_create_for_update(pid)
            product.add_stock(qty)
            uow.products.save(product)
            uow.commit()
            snapshot = product.snapshot()
        logger.info("stock.add product=%s qty=%s total=%s", pid, qty, snapshot.total)
        return snapshot

    def remove_stock(self, product_id: str, quantity: int) -> StockSnapshot:
        """Saída. Só consome unidades disponíveis."""
        pid = validate_identifier(product_id, "product_id")
        qty = validate_quantity(quantity)
        with self._uow_factory() as uow:
            product = self._locked_product(uow, pid)
            product.remove_stock(qty)
            uow.products.save(product)
            uow.commit()
            snapshot = product.snapshot()
        logger.info("stock.remove product=%s qty=%s total=%s", pid, qty, snapshot.total)
        return snapshot

    def reserve(self, product_id: str, quantity: int, request_id: str) -> Hold:
        pid = validate_identifier(product_id, "product_id")
        qty = validate_quantity(quantity)
        with self._uow_factory() as uow:
            product = self._locked_product(uow, pid)
            hold = product.reserve(qty, request_id, self._new_id(), self._now())
            uow.products.save(product)
            uow.holds.save(hold)
            uow.commit()
        logger.info("stock.reserve product=%s qty=%s request=%s hold=%s", pid, qty, request_id, hold.id)
        return hold

    def block(self, product_id: str, quantity: int, reason: str | None = None) -> Hold:
        pid = validate_identifier(product_id, "product_id")
        qty = validate_quantity(quantity)
        with self._uow_factory() as uow:
            product = self._locked_product(uow, pid)
            hold = product.block(qty, reason, self._new_id(), self._now())
            uow.products.save(product)
            uow.holds.save(hold)
            uow.commit()
        logger.info("stock.block product=%s qty=%s hold=%s", pid, qty, hold.id)
        return hold

    def release_reservation(self, reservation_id: str) -> Hold:
        return self._release(reservation_id, HoldKind.RESERVATION)

    def release_block(self, block_id: str) -> Hold:
        return self._release(block_id, HoldKind.BLOCK)

    # ------------------------------------------------------------------ #
    # Consultas (sem lock: leitura de estado confirmado)
    # ------------------------------------------------------------------ #
    def get_stock(self, product_id: str) -> StockSnapshot:
        pid = validate_identifier(product_id, "product_id")
        with self._uow_factory() as uow:
            product = uow.products.get(pid)
        if product is None:
            raise ProductNotFoundError(pid)
        return product.snapshot()

    def get_reservation(self, reservation_id: str) -> Hold:
        return self._get_hold(reservation_id, HoldKind.RESERVATION)

    def get_block(self, block_id: str) -> Hold:
        return self._get_hold(block_id, HoldKind.BLOCK)

    # ------------------------------------------------------------------ #
    # Internos
    # ------------------------------------------------------------------ #
    @staticmethod
    def _locked_product(uow: UnitOfWork, product_id: str) -> Product:
        product = uow.products.get_for_update(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        return product

    def _release(self, hold_id: str, kind: HoldKind) -> Hold:
        hid = validate_identifier(hold_id, "hold_id")
        with self._uow_factory() as uow:
            # 1) descobre o produto dono (leitura sem lock: o vínculo é imutável)
            product_id = uow.holds.find_product_id(hid)
            if product_id is None:
                raise HoldNotFoundError(kind.label, hid)

            # 2) trava o produto
            product = uow.products.get_for_update(product_id)
            if product is None:  # holds têm FK para produtos: não deveria ocorrer
                raise InvariantViolationError("Hold sem produto.", hold_id=hid, product_id=product_id)

            # 3) RE-LÊ o hold já sob lock. Sem isso, duas liberações concorrentes
            #    do mesmo hold poderiam ambas enxergar 'ACTIVE' e devolver as
            #    unidades duas vezes.
            hold = uow.holds.get(hid)
            if hold is None or hold.kind is not kind:
                raise HoldNotFoundError(kind.label, hid)

            released = product.release(hold, self._now())
            uow.products.save(product)
            uow.holds.save(released)
            uow.commit()
        logger.info("stock.release kind=%s hold=%s product=%s qty=%s", kind.value, hid, product_id, hold.quantity)
        return released

    def _get_hold(self, hold_id: str, kind: HoldKind) -> Hold:
        hid = validate_identifier(hold_id, "hold_id")
        with self._uow_factory() as uow:
            hold = uow.holds.get(hid)
        if hold is None or hold.kind is not kind:
            raise HoldNotFoundError(kind.label, hid)
        return hold
