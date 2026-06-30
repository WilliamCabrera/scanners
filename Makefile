.PHONY: help install run dashboard build up down restart logs logs-dashboard \
        logs-all refresh-daily shell clean

# ── Default ───────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  LOCAL"
	@echo "  make install          Install all dependencies (massive + dashboard)"
	@echo "  make run              Run terminal scanner locally"
	@echo "  make dashboard        Run Streamlit dashboard locally  →  :8505"
	@echo "  make refresh-daily    Manually refresh prev-day OHLC cache"
	@echo ""
	@echo "  DOCKER"
	@echo "  make build            Build Docker images"
	@echo "  make up               Start all services in background"
	@echo "  make down             Stop and remove containers"
	@echo "  make restart          Rebuild and restart all services"
	@echo "  make logs             Tail scanner logs"
	@echo "  make logs-dashboard   Tail dashboard logs"
	@echo "  make logs-all         Tail all service logs"
	@echo "  make shell            Open shell in scanner container"
	@echo ""
	@echo "  MISC"
	@echo "  make clean            Remove .cache files and __pycache__"
	@echo ""

# ── Local ─────────────────────────────────────────────────────────────────────

install:
	pip install -e ".[dashboard]"

run:
	python main.py --provider massive

dashboard:
	streamlit run dashboard.py

refresh-daily:
	python scripts/refresh_daily_store.py

# ── Docker ────────────────────────────────────────────────────────────────────

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose down
	docker compose up -d --build

logs:
	docker compose logs -f scanner

logs-dashboard:
	docker compose logs -f dashboard

logs-all:
	docker compose logs -f

shell:
	docker compose exec scanner bash

# ── Misc ──────────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .cache/*.json
