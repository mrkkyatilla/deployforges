.PHONY: dev setup db-up db-down migrate seed worker beat test lint format gcp-setup

# ── Development ──
dev:
	uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

worker:
	celery -A workers.build_worker worker --loglevel=info --concurrency=2

beat:
	celery -A workers.build_worker beat --loglevel=info

# ── Setup ──
setup:
	pip install -e ".[dev]"
	cp -n .env.example .env || true
	@echo ""
	@echo "✓ Setup complete. Next: edit .env, then 'make db-up && make migrate && make seed'"

db-up:
	docker compose up -d db redis

db-down:
	docker compose down

migrate:
	bash scripts/create_migration.sh

seed:
	python scripts/seed_dev.py

gcp-setup:
	bash scripts/setup_gcp.sh

# ── Quality ──
test:
	pytest tests/ -v --cov=core --cov=api

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

# ── Production ──
prod-up:
	docker compose -f docker-compose.prod.yml up -d

prod-down:
	docker compose -f docker-compose.prod.yml down

prod-logs:
	docker compose -f docker-compose.prod.yml logs -f api celery-worker
