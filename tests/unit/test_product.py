"""Testes unitários do agregado ``Product`` (puros, sem I/O)."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from stock.domain.exceptions import (
    HoldAlreadyReleasedError,
    InsufficientStockError,
    InvalidIdentifierError,
    InvalidQuantityError,
    InvariantViolationError,
)
from stock.domain.models import MAX_QUANTITY, HoldKind, HoldStatus, Product

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stocked(total: int = 100) -> Product:
    p = Product("sku-1")
    p.add_stock(total)
    return p


def state(p: Product) -> tuple[int, int, int, int]:
    return p.total, p.available, p.reserved, p.blocked


# --------------------------------------------------------------------------- #
# Caminho feliz
# --------------------------------------------------------------------------- #
def test_new_product_starts_empty():
    assert state(Product("sku-1")) == (0, 0, 0, 0)


def test_add_stock_increases_total_and_available():
    p = stocked(100)
    p.add_stock(25)
    assert state(p) == (125, 125, 0, 0)


def test_remove_stock_decreases_total_and_available():
    p = stocked(100)
    p.remove_stock(40)
    assert state(p) == (60, 60, 0, 0)


def test_reserve_moves_units_from_available_to_reserved_without_changing_total():
    p = stocked(100)
    hold = p.reserve(30, "order-1", "h1", NOW)
    assert state(p) == (100, 70, 30, 0)
    assert (hold.kind, hold.quantity, hold.reference, hold.status) == (
        HoldKind.RESERVATION, 30, "order-1", HoldStatus.ACTIVE,
    )


def test_block_moves_units_from_available_to_blocked_without_changing_total():
    p = stocked(100)
    hold = p.block(20, "avaria", "b1", NOW)
    assert state(p) == (100, 80, 0, 20)
    assert (hold.kind, hold.reference) == (HoldKind.BLOCK, "avaria")


def test_block_reason_is_optional():
    assert stocked().block(1, None, "b1", NOW).reference is None


def test_multiple_holds_coexist_even_for_different_requests():
    p = stocked(100)
    p.reserve(10, "order-1", "h1", NOW)
    p.reserve(15, "order-2", "h2", NOW)
    p.block(5, None, "b1", NOW)
    p.block(7, "inventário", "b2", NOW)
    assert state(p) == (100, 63, 25, 12)


@pytest.mark.parametrize("kind", ["reservation", "block"])
def test_release_returns_units_to_available(kind):
    p = stocked(100)
    hold = p.reserve(30, "o", "h1", NOW) if kind == "reservation" else p.block(30, None, "h1", NOW)
    released = p.release(hold, NOW)
    assert state(p) == (100, 100, 0, 0)
    assert released.status is HoldStatus.RELEASED and released.released_at == NOW
    assert hold.status is HoldStatus.ACTIVE  # o original é imutável


# --------------------------------------------------------------------------- #
# Limites (boundary values)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("op", ["remove", "reserve", "block"])
def test_can_use_exactly_the_available_quantity(op):
    p = stocked(10)
    {"remove": lambda: p.remove_stock(10),
     "reserve": lambda: p.reserve(10, "o", "h", NOW),
     "block": lambda: p.block(10, None, "h", NOW)}[op]()
    assert p.available == 0


@pytest.mark.parametrize("op", ["remove", "reserve", "block"])
def test_cannot_use_one_more_than_available(op):
    p = stocked(10)
    before = state(p)
    call = {"remove": lambda: p.remove_stock(11),
            "reserve": lambda: p.reserve(11, "o", "h", NOW),
            "block": lambda: p.block(11, None, "h", NOW)}[op]
    with pytest.raises(InsufficientStockError) as err:
        call()
    assert (err.value.details["requested"], err.value.details["available"]) == (11, 10)
    assert state(p) == before  # nenhuma mutação parcial


def test_cannot_withdraw_units_that_are_reserved_or_blocked():
    p = stocked(100)
    p.reserve(60, "o", "h1", NOW)
    p.block(30, None, "b1", NOW)
    with pytest.raises(InsufficientStockError):
        p.remove_stock(11)  # só 10 estão realmente disponíveis
    p.remove_stock(10)
    assert state(p) == (90, 0, 60, 30)


def test_reserve_and_block_compete_for_the_same_available_units():
    p = stocked(10)
    p.reserve(6, "o", "h1", NOW)
    with pytest.raises(InsufficientStockError):
        p.block(5, None, "b1", NOW)
    p.block(4, None, "b1", NOW)
    assert p.available == 0


def test_zero_stock_rejects_everything():
    p = Product("sku-1")
    with pytest.raises(InsufficientStockError):
        p.remove_stock(1)
    with pytest.raises(InsufficientStockError):
        p.reserve(1, "o", "h", NOW)


# --------------------------------------------------------------------------- #
# Entradas inválidas
# --------------------------------------------------------------------------- #
INVALID_QUANTITIES = [0, -1, -100, 1.5, "10", None, True, False, [1], MAX_QUANTITY + 1]


@pytest.mark.parametrize("quantity", INVALID_QUANTITIES, ids=repr)
@pytest.mark.parametrize("op", ["add", "remove", "reserve", "block"])
def test_invalid_quantity_is_rejected_by_every_command(op, quantity):
    p = stocked(100)
    before = state(p)
    commands = {
        "add": lambda: p.add_stock(quantity),
        "remove": lambda: p.remove_stock(quantity),
        "reserve": lambda: p.reserve(quantity, "o", "h", NOW),
        "block": lambda: p.block(quantity, None, "h", NOW),
    }
    with pytest.raises(InvalidQuantityError):
        commands[op]()
    assert state(p) == before


@pytest.mark.parametrize("bad_id", ["", " ", "a b", "a/b", "x" * 65, None, 123, "ação"])
def test_invalid_product_id_is_rejected(bad_id):
    with pytest.raises(InvalidIdentifierError):
        Product(bad_id)


@pytest.mark.parametrize("request_id", [None, "", "   ", "x" * 129])
def test_reserve_requires_a_valid_request_id(request_id):
    p = stocked(10)
    with pytest.raises(InvalidIdentifierError):
        p.reserve(1, request_id, "h", NOW)
    assert p.reserved == 0


# --------------------------------------------------------------------------- #
# Liberação
# --------------------------------------------------------------------------- #
def test_releasing_twice_is_rejected_and_does_not_double_credit():
    p = stocked(100)
    released = p.release(p.reserve(30, "o", "h1", NOW), NOW)
    before = state(p)
    with pytest.raises(HoldAlreadyReleasedError):
        p.release(released, NOW)
    assert state(p) == before == (100, 100, 0, 0)


def test_cannot_release_a_hold_of_another_product():
    p, other = stocked(100), Product("sku-2")
    other.add_stock(10)
    foreign = other.reserve(5, "o", "h1", NOW)
    with pytest.raises(InvariantViolationError):
        p.release(foreign, NOW)


def test_release_detects_counter_smaller_than_the_hold():
    p = stocked(100)
    hold = p.reserve(30, "o", "h1", NOW)
    corrupted = Product.restore("sku-1", 100, 10, 0)  # contador menor que a reserva ativa
    with pytest.raises(InvariantViolationError):
        corrupted.release(hold, NOW)


# --------------------------------------------------------------------------- #
# Invariantes na reidratação (dado corrompido é recusado, não "consertado")
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "total,reserved,blocked",
    [(5, 4, 3), (5, 6, 0), (-1, 0, 0), (5, -1, 0), (5, 0, -1), (5.0, 0, 0)],
)
def test_restore_rejects_inconsistent_state(total, reserved, blocked):
    with pytest.raises(InvariantViolationError):
        Product.restore("sku-1", total, reserved, blocked)


def test_restore_accepts_consistent_state():
    assert state(Product.restore("sku-1", 10, 4, 6)) == (10, 0, 4, 6)


def test_snapshot_reflects_current_state():
    p = stocked(100)
    p.reserve(30, "o", "h1", NOW)
    p.block(20, None, "b1", NOW)
    s = p.snapshot()
    assert (s.product_id, s.total, s.available, s.reserved, s.blocked) == ("sku-1", 100, 50, 30, 20)


# --------------------------------------------------------------------------- #
# Teste baseado em modelo: 2.000 operações aleatórias contra um modelo de referência
# --------------------------------------------------------------------------- #
def test_random_operation_sequence_matches_reference_model_and_never_breaks_invariants():
    rng = random.Random(20260507)  # semente fixa: determinístico e reproduzível
    p = Product("sku-rand")
    total = reserved = blocked = 0
    active = {}
    counter = 0

    for _ in range(2000):
        op, qty = rng.choice(["add", "remove", "reserve", "block", "release"]), rng.randint(1, 25)
        available = total - reserved - blocked

        if op == "add":
            p.add_stock(qty)
            total += qty
        elif op == "remove":
            if qty <= available:
                p.remove_stock(qty)
                total -= qty
            else:
                with pytest.raises(InsufficientStockError):
                    p.remove_stock(qty)
        elif op in ("reserve", "block"):
            counter += 1
            if qty <= available:
                if op == "reserve":
                    hold = p.reserve(qty, "req", f"h{counter}", NOW)
                    reserved += qty
                else:
                    hold = p.block(qty, None, f"h{counter}", NOW)
                    blocked += qty
                active[hold.id] = hold
            else:
                with pytest.raises(InsufficientStockError):
                    (p.reserve(qty, "req", "x", NOW) if op == "reserve" else p.block(qty, None, "x", NOW))
        elif active:
            hold = active.pop(rng.choice(sorted(active)))
            p.release(hold, NOW)
            if hold.kind is HoldKind.RESERVATION:
                reserved -= hold.quantity
            else:
                blocked -= hold.quantity

        assert (p.total, p.reserved, p.blocked) == (total, reserved, blocked)
        assert p.available == total - reserved - blocked >= 0
