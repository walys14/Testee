"""Testes de contrato do ``StockService``.

Rodam contra *todos* os backends (memória e, se houver DATABASE_URL, PostgreSQL):
o comportamento observável precisa ser idêntico independentemente da persistência.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stock.domain.exceptions import (
    HoldAlreadyReleasedError,
    HoldNotFoundError,
    InsufficientStockError,
    InvalidIdentifierError,
    InvalidQuantityError,
    InvariantViolationError,
    ProductNotFoundError,
)
from stock.domain.models import HoldKind, HoldStatus

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def view(snapshot) -> tuple[int, int, int, int]:
    return snapshot.total, snapshot.available, snapshot.reserved, snapshot.blocked


# --------------------------------------------------------------------------- #
# Ciclo de vida completo
# --------------------------------------------------------------------------- #
def test_full_lifecycle(service):
    assert view(service.add_stock("sku", 100)) == (100, 100, 0, 0)

    reservation = service.reserve("sku", 30, "order-1")
    block = service.block("sku", 20, "avaria")
    assert view(service.get_stock("sku")) == (100, 50, 30, 20)

    assert view(service.remove_stock("sku", 50)) == (50, 0, 30, 20)
    with pytest.raises(InsufficientStockError):
        service.remove_stock("sku", 1)  # tudo que sobrou está reservado/bloqueado

    released = service.release_reservation(reservation.id)
    assert released.status is HoldStatus.RELEASED
    assert view(service.get_stock("sku")) == (50, 30, 0, 20)

    service.release_block(block.id)
    assert view(service.get_stock("sku")) == (50, 50, 0, 0)


def test_first_entry_creates_the_product(service):
    with pytest.raises(ProductNotFoundError):
        service.get_stock("novo")
    service.add_stock("novo", 5)
    assert view(service.get_stock("novo")) == (5, 5, 0, 0)


def test_holds_are_queryable_by_id_with_their_data(service):
    service.add_stock("sku", 50)
    r = service.reserve("sku", 7, "order-9")
    b = service.block("sku", 3, "inventário")

    got_r, got_b = service.get_reservation(r.id), service.get_block(b.id)
    assert (got_r.product_id, got_r.kind, got_r.quantity, got_r.reference, got_r.status) == (
        "sku", HoldKind.RESERVATION, 7, "order-9", HoldStatus.ACTIVE,
    )
    assert (got_b.kind, got_b.quantity, got_b.reference) == (HoldKind.BLOCK, 3, "inventário")
    assert got_r.created_at.tzinfo is not None


def test_many_reservations_for_different_requests_are_independent(service):
    service.add_stock("sku", 100)
    r1 = service.reserve("sku", 10, "order-1")
    r2 = service.reserve("sku", 20, "order-2")
    r3 = service.reserve("sku", 30, "order-1")  # mesma solicitação, outra reserva: permitido
    assert len({r1.id, r2.id, r3.id}) == 3

    service.release_reservation(r2.id)
    assert view(service.get_stock("sku")) == (100, 60, 40, 0)
    assert service.get_reservation(r1.id).status is HoldStatus.ACTIVE
    assert service.get_reservation(r3.id).status is HoldStatus.ACTIVE
    assert service.get_reservation(r2.id).status is HoldStatus.RELEASED


# --------------------------------------------------------------------------- #
# Violações de regra: erro claro e estoque intacto
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operation", ["remove", "reserve", "block"])
def test_exceeding_available_fails_and_leaves_stock_untouched(service, operation):
    service.add_stock("sku", 10)
    service.reserve("sku", 4, "order-1")
    before = view(service.get_stock("sku"))

    call = {
        "remove": lambda: service.remove_stock("sku", 7),
        "reserve": lambda: service.reserve("sku", 7, "order-2"),
        "block": lambda: service.block("sku", 7),
    }[operation]
    with pytest.raises(InsufficientStockError) as err:
        call()

    assert (err.value.details["requested"], err.value.details["available"]) == (7, 6)
    assert view(service.get_stock("sku")) == before


@pytest.mark.parametrize("operation", ["remove", "reserve", "block", "get"])
def test_unknown_product_is_not_found(service, operation):
    call = {
        "remove": lambda: service.remove_stock("ghost", 1),
        "reserve": lambda: service.reserve("ghost", 1, "o"),
        "block": lambda: service.block("ghost", 1),
        "get": lambda: service.get_stock("ghost"),
    }[operation]
    with pytest.raises(ProductNotFoundError):
        call()


def test_invalid_input_is_rejected_before_touching_storage(service):
    with pytest.raises(InvalidQuantityError):
        service.add_stock("brand-new", 0)
    with pytest.raises(ProductNotFoundError):  # a entrada inválida NÃO criou o produto
        service.get_stock("brand-new")

    for bad in ("", "a b", "x" * 65):
        with pytest.raises(InvalidIdentifierError):
            service.add_stock(bad, 1)


def test_releasing_an_unknown_hold_is_not_found(service):
    with pytest.raises(HoldNotFoundError):
        service.release_reservation("does-not-exist")
    with pytest.raises(HoldNotFoundError):
        service.release_block("does-not-exist")
    with pytest.raises(HoldNotFoundError):
        service.get_reservation("does-not-exist")


def test_a_reservation_id_cannot_be_used_as_a_block_id_and_vice_versa(service):
    service.add_stock("sku", 10)
    r, b = service.reserve("sku", 2, "o"), service.block("sku", 3)
    with pytest.raises(HoldNotFoundError):
        service.release_block(r.id)
    with pytest.raises(HoldNotFoundError):
        service.release_reservation(b.id)
    with pytest.raises(HoldNotFoundError):
        service.get_block(r.id)
    assert view(service.get_stock("sku")) == (10, 5, 2, 3)  # nada mudou


def test_releasing_twice_fails_and_does_not_credit_twice(service):
    service.add_stock("sku", 10)
    r = service.reserve("sku", 4, "o")
    service.release_reservation(r.id)
    with pytest.raises(HoldAlreadyReleasedError):
        service.release_reservation(r.id)
    assert view(service.get_stock("sku")) == (10, 10, 0, 0)


# --------------------------------------------------------------------------- #
# Atomicidade e guarda de lock (infra)
# --------------------------------------------------------------------------- #
def test_exception_inside_unit_of_work_rolls_back_everything(backend, service):
    service.add_stock("sku", 10)

    with pytest.raises(RuntimeError):
        with backend.unit_of_work() as uow:
            product = uow.products.get_for_update("sku")
            hold = product.reserve(4, "req", "h-rollback", NOW)
            uow.products.save(product)
            uow.holds.save(hold)
            raise RuntimeError("falha antes do commit")

    assert view(service.get_stock("sku")) == (10, 10, 0, 0)
    with pytest.raises(HoldNotFoundError):
        service.get_reservation("h-rollback")


def test_unit_of_work_without_commit_persists_nothing(backend, service):
    service.add_stock("sku", 10)
    with backend.unit_of_work() as uow:
        product = uow.products.get_for_update("sku")
        product.remove_stock(10)
        uow.products.save(product)  # sem commit()
    assert view(service.get_stock("sku")) == (10, 10, 0, 0)


def test_failed_first_entry_does_not_leave_a_phantom_product(backend, service):
    with pytest.raises(RuntimeError):
        with backend.unit_of_work() as uow:
            uow.products.get_or_create_for_update("ghost")
            raise RuntimeError("falha")
    with pytest.raises(ProductNotFoundError):
        service.get_stock("ghost")


def test_writing_without_holding_the_product_lock_is_refused(backend, service):
    service.add_stock("sku", 10)
    with backend.unit_of_work() as uow:
        product = uow.products.get("sku")  # leitura sem lock
        product.remove_stock(1)
        with pytest.raises(InvariantViolationError):
            uow.products.save(product)
