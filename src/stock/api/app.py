"""API HTTP (FastAPI). Camada fina: traduz HTTP <-> casos de uso.

Os endpoints são ``def`` (síncronos) de propósito: o FastAPI os executa em um
threadpool limitado, o que combina com o driver síncrono e limita naturalmente o
número de requisições em voo (backpressure) em vez de acumular corrotinas.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt

from stock.application.service import StockService
from stock.config import Settings
from stock.domain.exceptions import (
    HoldAlreadyReleasedError,
    InsufficientStockError,
    InvalidIdentifierError,
    InvalidQuantityError,
    InvariantViolationError,
    NotFoundError,
    StockError,
    TransientError,
)
from stock.domain.models import Hold, StockSnapshot

logger = logging.getLogger("stock.api")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class QuantityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: StrictInt  # a regra de negócio (>0, máximo) é do domínio


class ReserveIn(QuantityIn):
    request_id: str


class BlockIn(QuantityIn):
    reason: str | None = None


class StockOut(BaseModel):
    product_id: str
    total: int
    available: int
    reserved: int
    blocked: int

    @classmethod
    def from_snapshot(cls, s: StockSnapshot) -> StockOut:
        return cls(**s.__dict__)


class HoldOut(BaseModel):
    id: str
    product_id: str
    kind: str
    quantity: int
    reference: str | None
    status: str
    created_at: str
    released_at: str | None

    @classmethod
    def from_hold(cls, h: Hold) -> HoldOut:
        return cls(
            id=h.id, product_id=h.product_id, kind=h.kind.value, quantity=h.quantity, reference=h.reference,
            status=h.status.value, created_at=h.created_at.isoformat(),
            released_at=h.released_at.isoformat() if h.released_at else None,
        )


# --------------------------------------------------------------------------- #
# Mapeamento exceção de domínio -> HTTP (o primeiro match vence)
# --------------------------------------------------------------------------- #
_STATUS_BY_EXCEPTION: list[tuple[type[StockError], int]] = [
    (NotFoundError, 404),
    (InsufficientStockError, 409),        # conflito com o estado atual do estoque
    (HoldAlreadyReleasedError, 409),
    (InvalidQuantityError, 422),
    (InvalidIdentifierError, 422),
    (TransientError, 503),                # lock timeout, deadlock, banco indisponível
    (InvariantViolationError, 500),
    (StockError, 400),
]


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StockError)
    async def _stock_error(_: Request, exc: StockError) -> JSONResponse:
        status = next(code for cls, code in _STATUS_BY_EXCEPTION if isinstance(exc, cls))
        headers = {}
        if isinstance(exc, InvariantViolationError):
            logger.error("Invariante violada: %s %s", exc.message, exc.details, exc_info=exc)
            return JSONResponse(_error_body(exc.code, "Erro interno de consistência."), status_code=500)
        if isinstance(exc, TransientError):
            headers["Retry-After"] = "1"
            logger.warning("Falha transitória: %s (%s)", exc.code, exc.message)
        return JSONResponse(_error_body(exc.code, exc.message, exc.details), status_code=status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            _error_body("invalid_request", "Requisição inválida.", {"errors": errors}), status_code=422
        )


# --------------------------------------------------------------------------- #
# Fábrica da aplicação
# --------------------------------------------------------------------------- #
def build_service(settings: Settings):
    """Cria o backend escolhido e retorna (service, backend)."""
    if settings.backend == "postgres":
        from stock.infrastructure.postgres import PostgresBackend

        if not settings.database_url:
            raise RuntimeError("DATABASE_URL é obrigatório para STOCK_BACKEND=postgres.")
        backend = PostgresBackend(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            pool_timeout_s=settings.db_pool_timeout_s,
            lock_timeout_ms=settings.db_lock_timeout_ms,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
        backend.open()
        backend.ensure_schema()
    elif settings.backend == "memory":
        from stock.infrastructure.memory import InMemoryBackend

        backend = InMemoryBackend(lock_timeout_s=settings.memory_lock_timeout_s)
    else:
        raise RuntimeError(f"STOCK_BACKEND inválido: {settings.backend!r} (use 'memory' ou 'postgres').")
    return StockService(backend.unit_of_work), backend


def get_service(request: Request) -> StockService:
    return request.app.state.service


ServiceDep = Annotated[StockService, Depends(get_service)]


def create_app(service: StockService | None = None, settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        backend = None
        if service is not None:  # injeção direta (testes)
            app.state.service = service
        else:
            app.state.service, backend = build_service(settings or Settings.from_env())
        yield
        if backend is not None:
            backend.close()

    app = FastAPI(title="Controle de Estoque", version="1.0.0", lifespan=lifespan)
    _install_error_handlers(app)

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        return {"status": "ok"}

    # -- comandos ---------------------------------------------------------- #
    @app.post("/products/{product_id}/entries", response_model=StockOut, tags=["estoque"])
    def add_stock(product_id: str, body: QuantityIn, svc: ServiceDep) -> StockOut:
        return StockOut.from_snapshot(svc.add_stock(product_id, body.quantity))

    @app.post("/products/{product_id}/withdrawals", response_model=StockOut, tags=["estoque"])
    def remove_stock(product_id: str, body: QuantityIn, svc: ServiceDep) -> StockOut:
        return StockOut.from_snapshot(svc.remove_stock(product_id, body.quantity))

    @app.post("/products/{product_id}/reservations", response_model=HoldOut, status_code=201, tags=["reservas"])
    def reserve(product_id: str, body: ReserveIn, svc: ServiceDep) -> HoldOut:
        return HoldOut.from_hold(svc.reserve(product_id, body.quantity, body.request_id))

    @app.post("/products/{product_id}/blocks", response_model=HoldOut, status_code=201, tags=["bloqueios"])
    def block(product_id: str, body: BlockIn, svc: ServiceDep) -> HoldOut:
        return HoldOut.from_hold(svc.block(product_id, body.quantity, body.reason))

    @app.post("/reservations/{reservation_id}/release", response_model=HoldOut, tags=["reservas"])
    def release_reservation(reservation_id: str, svc: ServiceDep) -> HoldOut:
        return HoldOut.from_hold(svc.release_reservation(reservation_id))

    @app.post("/blocks/{block_id}/release", response_model=HoldOut, tags=["bloqueios"])
    def release_block(block_id: str, svc: ServiceDep) -> HoldOut:
        return HoldOut.from_hold(svc.release_block(block_id))

    # -- consultas --------------------------------------------------------- #
    @app.get("/products/{product_id}", response_model=StockOut, tags=["estoque"])
    def get_stock(product_id: str, svc: ServiceDep) -> StockOut:
        return StockOut.from_snapshot(svc.get_stock(product_id))

    @app.get("/reservations/{reservation_id}", response_model=HoldOut, tags=["reservas"])
    def get_reservation(reservation_id: str, svc: ServiceDep) -> HoldOut:
        return HoldOut.from_hold(svc.get_reservation(reservation_id))

    @app.get("/blocks/{block_id}", response_model=HoldOut, tags=["bloqueios"])
    def get_block(block_id: str, svc: ServiceDep) -> HoldOut:
        return HoldOut.from_hold(svc.get_block(block_id))

    return app
