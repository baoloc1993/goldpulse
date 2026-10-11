# GoldPulse — build / test / run
# Run artifacts live in deployment/ (local DB, production compose, .env.example).

.PHONY: help db dev check test migrate deploy doctor brain

UV ?= uv
COMPOSE = docker compose -f deployment/local/docker-compose.yml
PROD = docker compose -f deployment/production/docker-compose.yml --env-file deployment/production/.env

help:
	@echo "GoldPulse"
	@echo "  make dev      local: db + migrate + web (:8000) + worker + brain"
	@echo "  make check    lint + format check + import smoke test"
	@echo "  make test     pytest (starts the local Postgres first)"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make deploy   check + test, then build and run the production compose stack"
	@echo "  make doctor   check env vars and connectivity (DB, gold-api, Dukascopy, FRED, brain)"
	@echo "  make brain    run the chat/prediction brain on the host (BRAIN_BACKEND=cli|api|fake)"

db:
	$(COMPOSE) up -d --wait db

dev: db
	$(UV) run python -m goldpulse.devserver

check:
	$(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run python -c "import goldpulse.main, goldpulse.worker.__main__"

test: db
	$(UV) run pytest -q

migrate: db
	$(UV) run alembic upgrade head

# Stop `make dev` first: two workers must not write to the same database.
deploy: check test
	$(PROD) up -d --build --remove-orphans && $(PROD) ps

doctor:
	$(UV) run python -m goldpulse.doctor

# The brain runs on the host, outside Docker: the `cli` backend needs a local Claude Code login.
# --production forces APP_ENV=production (the `fake` backend is refused).
brain:
	$(UV) run python -m goldpulse.brain --production
