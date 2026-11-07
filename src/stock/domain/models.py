"""Modelo de domínio do estoque.

Decisões centrais
-----------------
* **Fronteira de consistência = um produto.** ``Product`` é o agregado; todas as
  invariantes de quantidade vivem nele e só mudam por seus métodos.
* **``total`` é a fonte da verdade; ``available`` é derivado**
  (``total - reserved - blocked``). Não existe um contador "disponível" que possa
  divergir dos demais.
* **Reservas e bloqueios (``Hold``) são entidades separadas do agregado.** O
  agregado guarda apenas contadores (tamanho O(1) independentemente de quantas
  reservas existam); cada ``Hold`` é carregado individualmente pelo seu id.
* **Check-then-act, sem mutação parcial.** Todo método valida *antes* de mutar.
  Se algo falhar, o objeto continua exatamente como estava.
* Este módulo é puro: sem I/O, sem relógio, sem gerador de ids (são injetados).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .exceptions import (
    HoldAlreadyReleasedError,
    InsufficientStockError,
    InvalidIdentifierError,
    InvalidQuantityError,
    InvariantViolationError,
)

MAX_QUANTITY = 1_000_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,64}")
MAX_REQUEST_ID_LENGTH = 128
MAX_REASON_LENGTH = 256


# --------------------------------------------------------------------------- #
# Validação de entrada (fail-fast, antes de qualquer lock)
# --------------------------------------------------------------------------- #
def validate_quantity(quantity: object) -> int:
    # bool é subclasse de int em Python: True/False não são quantidades válidas.
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise InvalidQuantityError("A quantidade deve ser um número inteiro.", received=repr(quantity))
    if quantity <= 0:
        raise InvalidQuantityError("A quantidade deve ser maior que zero.", received=quantity)
    if quantity > MAX_QUANTITY:
        raise InvalidQuantityError(
            f"A quantidade máxima por operação é {MAX_QUANTITY}.", received=quantity, maximum=MAX_QUANTITY
        )
    return quantity


def validate_identifier(value: object, field: str = "identificador") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise InvalidIdentifierError(
            f"{field} inválido: use de 1 a 64 caracteres entre letras, dígitos e . _ : -",
            field=field,
        )
    return value


def _validate_reference(value: str | None, *, field: str, required: bool, max_length: int) -> str | None:
    if value is None:
        if required:
            raise InvalidIdentifierError(f"{field} é obrigatório.", field=field)
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise InvalidIdentifierError(
            f"{field} inválido: texto não vazio com até {max_length} caracteres.", field=field
        )
    return value


# --------------------------------------------------------------------------- #
# Hold: reserva ou bloqueio
# --------------------------------------------------------------------------- #
class HoldKind(str, Enum):
    RESERVATION = "reservation"
    BLOCK = "block"

    @property
    def label(self) -> str:
        return "reserva" if self is HoldKind.RESERVATION else "bloqueio"


class HoldStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"


@dataclass(frozen=True)
class Hold:
    """Reserva (vinculada a uma solicitação) ou bloqueio (com motivo opcional).

    Imutável: liberar produz uma *nova* instância com status ``RELEASED``. O
    registro nunca é apagado, o que preserva auditoria e permite distinguir
    "nunca existiu" (404) de "já liberado" (409).
    """

    id: str
    product_id: str
    kind: HoldKind
    quantity: int
    reference: str | None  # request_id (reserva) ou motivo (bloqueio)
    status: HoldStatus
    created_at: datetime
    released_at: datetime | None = None


@dataclass(frozen=True)
class StockSnapshot:
    product_id: str
    total: int
    available: int
    reserved: int
    blocked: int


# --------------------------------------------------------------------------- #
# Agregado Product
# --------------------------------------------------------------------------- #
class Product:
    """Agregado de estoque de um produto.

    Invariantes (verificadas após cada mutação e na reidratação):
        total >= 0, reserved >= 0, blocked >= 0
        reserved + blocked <= total        (equivale a available >= 0)

    O agregado **não é thread-safe por si só**: a exclusão mútua é
    responsabilidade da camada de aplicação/infra (lock pessimista no produto).
    Se uma verificação de invariante falhar *depois* de mutar, o objeto fica
    descartável: a UnitOfWork faz rollback e ele nunca é persistido.
    """

    __slots__ = ("_id", "_total", "_reserved", "_blocked")

    def __init__(self, product_id: str) -> None:
        self._id = validate_identifier(product_id, "product_id")
        self._total = 0
        self._reserved = 0
        self._blocked = 0

    @classmethod
    def restore(cls, product_id: str, total: int, reserved: int, blocked: int) -> Product:
        """Reidrata do armazenamento; recusa estado já corrompido (fail-fast)."""
        product = cls(product_id)
        product._total, product._reserved, product._blocked = total, reserved, blocked
        product._check_invariants()
        return product

    # -- leitura ----------------------------------------------------------- #
    @property
    def id(self) -> str:
        return self._id

    @property
    def total(self) -> int:
        return self._total

    @property
    def reserved(self) -> int:
        return self._reserved

    @property
    def blocked(self) -> int:
        return self._blocked

    @property
    def available(self) -> int:
        return self._total - self._reserved - self._blocked

    def snapshot(self) -> StockSnapshot:
        return StockSnapshot(self._id, self._total, self.available, self._reserved, self._blocked)

    # -- comandos ---------------------------------------------------------- #
    def add_stock(self, quantity: int) -> None:
        """Entrada: aumenta o total (e, portanto, o disponível)."""
        qty = validate_quantity(quantity)
        self._total += qty
        self._check_invariants()

    def remove_stock(self, quantity: int) -> None:
        """Saída: só consome unidades *disponíveis* (nunca reservadas/bloqueadas)."""
        qty = validate_quantity(quantity)
        self._require_available(qty, "retirar")
        self._total -= qty
        self._check_invariants()

    def reserve(self, quantity: int, request_id: str, hold_id: str, now: datetime) -> Hold:
        qty = validate_quantity(quantity)
        ref = _validate_reference(request_id, field="request_id", required=True, max_length=MAX_REQUEST_ID_LENGTH)
        hold_id = validate_identifier(hold_id, "hold_id")
        self._require_available(qty, "reservar")
        self._reserved += qty
        self._check_invariants()
        return Hold(hold_id, self._id, HoldKind.RESERVATION, qty, ref, HoldStatus.ACTIVE, now)

    def block(self, quantity: int, reason: str | None, hold_id: str, now: datetime) -> Hold:
        qty = validate_quantity(quantity)
        ref = _validate_reference(reason, field="reason", required=False, max_length=MAX_REASON_LENGTH)
        hold_id = validate_identifier(hold_id, "hold_id")
        self._require_available(qty, "bloquear")
        self._blocked += qty
        self._check_invariants()
        return Hold(hold_id, self._id, HoldKind.BLOCK, qty, ref, HoldStatus.ACTIVE, now)

    def release(self, hold: Hold, now: datetime) -> Hold:
        """Libera uma reserva/bloqueio ATIVO, devolvendo as unidades ao disponível."""
        if hold.product_id != self._id:
            raise InvariantViolationError(
                "Hold pertence a outro produto.", hold_id=hold.id, hold_product=hold.product_id, product=self._id
            )
        if hold.status is HoldStatus.RELEASED:
            raise HoldAlreadyReleasedError(hold.id)

        if hold.kind is HoldKind.RESERVATION:
            if self._reserved < hold.quantity:
                raise InvariantViolationError("Contador de reservas menor que a reserva ativa.", hold_id=hold.id)
            self._reserved -= hold.quantity
        else:
            if self._blocked < hold.quantity:
                raise InvariantViolationError("Contador de bloqueios menor que o bloqueio ativo.", hold_id=hold.id)
            self._blocked -= hold.quantity

        self._check_invariants()
        return replace(hold, status=HoldStatus.RELEASED, released_at=now)

    # -- internos ---------------------------------------------------------- #
    def _require_available(self, quantity: int, operation: str) -> None:
        if quantity > self.available:
            raise InsufficientStockError(operation, quantity, self.available)

    def _check_invariants(self) -> None:
        values = (self._total, self._reserved, self._blocked)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
            raise InvariantViolationError(
                "Quantidades devem ser inteiros não negativos.",
                product_id=self._id, total=self._total, reserved=self._reserved, blocked=self._blocked,
            )
        if self._reserved + self._blocked > self._total:
            raise InvariantViolationError(
                "reservado + bloqueado excede o total.",
                product_id=self._id, total=self._total, reserved=self._reserved, blocked=self._blocked,
            )

    def __repr__(self) -> str:  # útil em logs e falhas de teste
        return (
            f"Product(id={self._id!r}, total={self._total}, available={self.available}, "
            f"reserved={self._reserved}, blocked={self._blocked})"
        )
