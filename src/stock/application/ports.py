"""Portas (interfaces) que a camada de aplicação exige da infraestrutura.

O contrato mais importante está em ``get_for_update``: **o lock obtido nele só é
liberado quando a UnitOfWork termina** (commit ou rollback). É isso que
transforma "ler → decidir → gravar" em uma seção crítica por produto.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from stock.domain.models import Hold, Product


class ProductRepository(Protocol):
    def get(self, product_id: str) -> Product | None:
        """Leitura sem lock (consultas). Enxerga apenas estado já confirmado."""

    def get_for_update(self, product_id: str) -> Product | None:
        """Carrega o produto sob **lock pessimista** até o fim da UnitOfWork.

        Retorna ``None`` se não existir (sem criar nem travar nada).
        Levanta ``LockTimeoutError`` se não conseguir o lock no prazo.
        """

    def get_or_create_for_update(self, product_id: str) -> Product:
        """Idem, mas cria o produto (zerado) de forma atômica se ainda não existir."""

    def save(self, product: Product) -> None:
        """Persiste o estado. Exige que o lock do produto esteja em posse da UoW."""


class HoldRepository(Protocol):
    def find_product_id(self, hold_id: str) -> str | None:
        """Descobre o produto de um hold (leitura sem lock; holds nunca são apagados
        nem trocam de produto, então a leitura não precisa ser travada)."""

    def get(self, hold_id: str) -> Hold | None: ...

    def save(self, hold: Hold) -> None:
        """Insere ou atualiza (status/released_at). Exige o lock do produto dono."""


class UnitOfWork(Protocol):
    products: ProductRepository
    holds: HoldRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, exc_type, exc, tb) -> None:
        """Sempre libera os locks; faz rollback se ``commit`` não foi chamado."""

    def commit(self) -> None:
        """Torna atômica e durável a unidade de trabalho (produto + holds)."""


UnitOfWorkFactory = Callable[[], UnitOfWork]
