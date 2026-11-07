"""Configuração via variáveis de ambiente (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    backend: str = "memory"  # "memory" | "postgres"
    database_url: str | None = None
    db_pool_min: int = 2
    db_pool_max: int = 10
    db_pool_timeout_s: float = 5.0
    db_lock_timeout_ms: int = 2000
    db_statement_timeout_ms: int = 5000
    memory_lock_timeout_s: float = 5.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        database_url = e.get("DATABASE_URL") or None
        return cls(
            backend=e.get("STOCK_BACKEND", "postgres" if database_url else "memory").lower(),
            database_url=database_url,
            db_pool_min=int(e.get("DB_POOL_MIN", cls.db_pool_min)),
            db_pool_max=int(e.get("DB_POOL_MAX", cls.db_pool_max)),
            db_pool_timeout_s=float(e.get("DB_POOL_TIMEOUT_S", cls.db_pool_timeout_s)),
            db_lock_timeout_ms=int(e.get("DB_LOCK_TIMEOUT_MS", cls.db_lock_timeout_ms)),
            db_statement_timeout_ms=int(e.get("DB_STATEMENT_TIMEOUT_MS", cls.db_statement_timeout_ms)),
            memory_lock_timeout_s=float(e.get("MEMORY_LOCK_TIMEOUT_S", cls.memory_lock_timeout_s)),
        )
