"""Testes da camada HTTP (backend em memória): contrato de status e formato de erro."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stock.api.app import create_app
from stock.application.service import StockService
from stock.domain.exceptions import InvariantViolationError, LockTimeoutError
from stock.infrastructure.memory import InMemoryBackend


@pytest.fixture
def client():
    service = StockService(InMemoryBackend().unit_of_work)
    with TestClient(create_app(service=service)) as c:
        yield c


def stock(client, product_id="sku"):
    return client.get(f"/products/{product_id}").json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_end_to_end_flow(client):
    assert client.post("/products/sku/entries", json={"quantity": 100}).json() == {
        "product_id": "sku", "total": 100, "available": 100, "reserved": 0, "blocked": 0,
    }

    r = client.post("/products/sku/reservations", json={"quantity": 30, "request_id": "order-1"})
    assert r.status_code == 201
    reservation = r.json()
    assert (reservation["kind"], reservation["status"], reservation["reference"]) == ("reservation", "active", "order-1")

    b = client.post("/products/sku/blocks", json={"quantity": 20, "reason": "avaria"})
    assert b.status_code == 201
    block = b.json()

    assert stock(client) == {"product_id": "sku", "total": 100, "available": 50, "reserved": 30, "blocked": 20}

    assert client.post("/products/sku/withdrawals", json={"quantity": 51}).status_code == 409
    assert client.post("/products/sku/withdrawals", json={"quantity": 50}).status_code == 200

    released = client.post(f"/reservations/{reservation['id']}/release")
    assert released.status_code == 200 and released.json()["status"] == "released"
    assert client.get(f"/reservations/{reservation['id']}").json()["status"] == "released"
    assert client.post(f"/blocks/{block['id']}/release").status_code == 200

    assert stock(client) == {"product_id": "sku", "total": 50, "available": 50, "reserved": 0, "blocked": 0}


def test_insufficient_stock_is_409_with_structured_details(client):
    client.post("/products/sku/entries", json={"quantity": 5})
    r = client.post("/products/sku/reservations", json={"quantity": 6, "request_id": "o"})
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"] == {"operation": "reservar", "requested": 6, "available": 5}
    assert stock(client)["reserved"] == 0


def test_not_found_is_404(client):
    assert client.get("/products/ghost").status_code == 404
    assert client.post("/products/ghost/withdrawals", json={"quantity": 1}).json()["error"]["code"] == "product_not_found"
    assert client.post("/reservations/nope/release").json()["error"]["code"] == "hold_not_found"
    assert client.get("/blocks/nope").status_code == 404


def test_releasing_twice_is_409(client):
    client.post("/products/sku/entries", json={"quantity": 5})
    rid = client.post("/products/sku/reservations", json={"quantity": 2, "request_id": "o"}).json()["id"]
    assert client.post(f"/reservations/{rid}/release").status_code == 200
    again = client.post(f"/reservations/{rid}/release")
    assert again.status_code == 409 and again.json()["error"]["code"] == "hold_already_released"


@pytest.mark.parametrize("quantity", [0, -3])
def test_non_positive_quantity_is_422_domain_error(client, quantity):
    r = client.post("/products/sku/entries", json={"quantity": quantity})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_quantity"


@pytest.mark.parametrize("body", [{"quantity": "10"}, {"quantity": 1.5}, {"quantity": True}, {}, {"quantity": 1, "extra": 1}])
def test_malformed_body_is_422_with_uniform_envelope(client, body):
    r = client.post("/products/sku/entries", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["details"]["errors"]


def test_reserve_requires_request_id(client):
    client.post("/products/sku/entries", json={"quantity": 5})
    assert client.post("/products/sku/reservations", json={"quantity": 1}).status_code == 422
    assert client.post("/products/sku/reservations", json={"quantity": 1, "request_id": "  "}).status_code == 422


def test_invalid_product_id_is_422(client):
    r = client.post("/products/bad id!/entries", json={"quantity": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_identifier"


# --------------------------------------------------------------------------- #
# Falhas transitórias e invariantes
# --------------------------------------------------------------------------- #
class _FailingService:
    def __init__(self, exc):
        self._exc = exc

    def reserve(self, *args, **kwargs):
        raise self._exc


def test_transient_failure_is_503_with_retry_after():
    app = create_app(service=_FailingService(LockTimeoutError("timeout", product_id="sku")))
    with TestClient(app) as c:
        r = c.post("/products/sku/reservations", json={"quantity": 1, "request_id": "o"})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "lock_timeout"


def test_invariant_violation_is_500_without_leaking_internals():
    app = create_app(service=_FailingService(InvariantViolationError("segredo interno", total=1)))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/products/sku/reservations", json={"quantity": 1, "request_id": "o"})
    assert r.status_code == 500
    assert "segredo interno" not in r.text
    assert r.json()["error"]["code"] == "invariant_violation"
