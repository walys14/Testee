"""Exceções do domínio de estoque.

A hierarquia separa *quem errou* e *o que o chamador pode fazer*:

    StockError
    ├── BusinessRuleViolation   -> o pedido viola uma regra (não adianta repetir igual)
    │   ├── InvalidQuantityError
    │   ├── InvalidIdentifierError
    │   ├── InsufficientStockError
    │   └── HoldAlreadyReleasedError
    ├── NotFoundError           -> o recurso referenciado não existe
    │   ├── ProductNotFoundError
    │   └── HoldNotFoundError
    ├── InvariantViolationError -> bug ou dado corrompido (nunca causado pelo cliente)
    └── TransientError          -> falha operacional; repetir *pode* funcionar
        ├── LockTimeoutError
        ├── ConcurrencyConflictError
        └── StorageUnavailableError

Toda exceção carrega ``code`` (estável, para máquinas) e ``details`` (contexto
estruturado, para logs e respostas de API). A mensagem é para humanos.
"""

from __future__ import annotations

from typing import Any


class StockError(Exception):
    code = "stock_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details


# --------------------------------------------------------------------------- #
# Violações de regra de negócio (erro do chamador)
# --------------------------------------------------------------------------- #
class BusinessRuleViolation(StockError):
    code = "business_rule_violation"


class InvalidQuantityError(BusinessRuleViolation):
    code = "invalid_quantity"


class InvalidIdentifierError(BusinessRuleViolation):
    code = "invalid_identifier"


class InsufficientStockError(BusinessRuleViolation):
    code = "insufficient_stock"

    def __init__(self, operation: str, requested: int, available: int) -> None:
        super().__init__(
            f"Não é possível {operation} {requested} unidade(s): "
            f"apenas {available} disponível(is).",
            operation=operation,
            requested=requested,
            available=available,
        )


class HoldAlreadyReleasedError(BusinessRuleViolation):
    code = "hold_already_released"

    def __init__(self, hold_id: str) -> None:
        super().__init__(f"'{hold_id}' já foi liberado.", hold_id=hold_id)


# --------------------------------------------------------------------------- #
# Recursos inexistentes
# --------------------------------------------------------------------------- #
class NotFoundError(StockError):
    code = "not_found"


class ProductNotFoundError(NotFoundError):
    code = "product_not_found"

    def __init__(self, product_id: str) -> None:
        super().__init__(f"Produto '{product_id}' não encontrado.", product_id=product_id)


class HoldNotFoundError(NotFoundError):
    code = "hold_not_found"

    def __init__(self, kind: str, hold_id: str) -> None:
        super().__init__(f"{kind.capitalize()} '{hold_id}' não encontrada.", kind=kind, hold_id=hold_id)


# --------------------------------------------------------------------------- #
# Invariante violada: nunca deve acontecer; se acontecer, a operação é abortada
# --------------------------------------------------------------------------- #
class InvariantViolationError(StockError):
    code = "invariant_violation"


# --------------------------------------------------------------------------- #
# Falhas transitórias de infraestrutura (o cliente pode tentar de novo)
# --------------------------------------------------------------------------- #
class TransientError(StockError):
    code = "transient_error"
    retryable = True


class LockTimeoutError(TransientError):
    """Não foi possível obter o lock do produto dentro do prazo (fail-fast)."""

    code = "lock_timeout"


class ConcurrencyConflictError(TransientError):
    """Conflito de concorrência detectado pelo banco (ex.: deadlock)."""

    code = "concurrency_conflict"


class StorageUnavailableError(TransientError):
    """Banco/pool indisponível ou saturado."""

    code = "storage_unavailable"
