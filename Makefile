.PHONY: up down test test-local run help

help:
	@echo "make up          - sobe PostgreSQL + API (http://localhost:8000)"
	@echo "make test        - roda a suite completa em Docker"
	@echo "make down        - derruba tudo, incluindo o volume do banco"
	@echo "make run         - roda a API local com backend em memoria"
	@echo "make test-local  - roda a suite local (pytest)"

up:
	docker compose up --build

down:
	docker compose down -v

test:
	docker compose --profile test run --rm --build tests

run:
	uvicorn stock.api.app:create_app --factory --app-dir src --reload

test-local:
	pytest
