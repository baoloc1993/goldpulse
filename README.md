# GoldPulse

GoldPulse is a self-hosted market-data and signal platform for gold (XAUUSD) and, optionally, Bitcoin (BTCUSDT).
A background worker builds a 1-minute candle warehouse from free public sources (2004 to today) and keeps it
current with a realtime price feed. It computes 15-minute technical signals and runs a logistic-regression model,
then records directional predictions that are scored automatically. Predictions drive simulated ("paper")
trading accounts with fixed daily budgets. A password-protected web dashboard shows data coverage, predictions,
Fed rate decisions and paper accounts. It also has a chat assistant that answers questions about the data
using Claude. The UI is in Vietnamese.

> This project is a personal engineering and research tool. Nothing it produces is financial advice.

## Features

- **Historical ingestion**: downloads and decodes Dukascopy `.bi5` files (LZMA, 2004 to 2009) and
  HistData.com CSV archives (2009 to today). Crawl state is tracked per day, with retry, throttling and an optional proxy.
- **Realtime feed**: polls gold-api.com and builds provisional 1-minute candles only while the market is open.
  A daily backfill later replaces them with final data.
- **Market calendar and sessions**: New York trading calendar with the daily break, plus Asia/Europe/US session
  bars computed with correct DST handling (`zoneinfo`).
- **Monitoring dashboard**: ingestion overview, per-day coverage (with manual retry), ad-hoc session queries
  and realtime lag thresholds.
- **Signals and model**: Bollinger %b, streak and RSI(9) signals on 15-minute bars. A pure-Python logistic
  model (coefficients in `data/models/`) predicts only on its most confident bars. A parity script checks the
  production implementation bar by bar against the pandas research version.
- **Predictions**: automatic 15-minute direction predictions, scored against the realised price
  (`up` / `down` / `flat` / `unscored`), with hit-rate statistics.
- **Fed context**: FOMC meeting calendar plus the FRED target-rate series, with the gold price reaction
  around each decision.
- **Chat assistant ("brain")**: a separate process reads jobs from a Postgres queue (`SKIP LOCKED`). For each
  job it builds a fixed data pack (no tools) and asks Claude through one of three backends: the Claude Code CLI,
  the Anthropic API, or a deterministic fake used in tests. The web app never calls the LLM directly, so it
  stays up when the brain is down.
- **Paper trading**: four XAUUSD paper accounts (A–D), each with its own entry rule (every signal, volatility
  filter, confidence filter, longer horizon). Accounts have a daily budget, a stop-out and a daily ledger, and
  entries and exits are sent as Telegram notifications.
- **BTCUSDT instance (optional)**: Binance 1-minute klines, 1-hour features, a BTC logistic model and four
  strategies (model, model + trend, spike, squeeze) on leveraged paper accounts. It has its own database,
  compose stack and Telegram bot.
- **Hardening**: a single operator access key (HMAC-signed session cookie), login rate limiting (SlowAPI),
  security headers and CSP, a request size limit, structured JSON logs, optional Sentry and daily `pg_dump`
  backups.

## Tech stack

Python 3.12 · FastAPI · Uvicorn · SQLModel / SQLAlchemy (async, asyncpg) · Alembic · PostgreSQL 16 ·
httpx · Jinja2 + vanilla JS/CSS · structlog · SlowAPI · Anthropic SDK / Claude Code CLI · pytest +
pytest-asyncio · Ruff · uv · Docker / Docker Compose.

## Architecture

```
  Dukascopy · HistData · gold-api · FRED · Binance        Claude (CLI | API | fake)
                     │                                              ▲
                     ▼                                              │
              ┌─────────────┐        ┌──────────────┐        ┌─────────────┐
  Telegram ◄──│   worker    │──────► │  PostgreSQL  │ ◄──────│    brain    │
              └─────────────┘        └──────────────┘        └─────────────┘
                                            ▲
                                            │
                                     ┌─────────────┐
                        browser ───► │ web (FastAPI)│
                                     └─────────────┘
```

Three processes share one PostgreSQL database and communicate only through it:

- `goldpulse.worker` runs the long-lived async loops: history load, realtime poll, backfill, signals,
  prediction scoring, Fed data, paper accounts, Telegram and the BTC loops. A Postgres advisory lock
  makes sure only one worker runs at a time.
- `goldpulse.brain` is the LLM job consumer and sends a heartbeat. It runs on the host when it uses the CLI backend.
- `goldpulse.main:app` is the FastAPI app that serves the HTML pages and a JSON API.

The code is layered: `integrations/` (external HTTP clients, no DB) → `repositories/` (SQL) → `services/`
(domain logic, no HTTP) → `api/` and `worker/`.

## Running locally

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (for PostgreSQL) and, optionally, `make`.

```bash
cp deployment/.env.example deployment/local/.env
# edit deployment/local/.env, at minimum:
#   DATABASE_URL=postgresql+asyncpg://goldpulse:goldpulse@127.0.0.1:5432/goldpulse
#   OPS_ACCESS_KEY=<random string, 16+ chars>
#   BRAIN_BACKEND=fake        # or cli / api (api needs ANTHROPIC_API_KEY)

uv sync
make dev        # starts Postgres, runs migrations, then web + worker + brain
```

Without `make`:

```bash
docker compose -f deployment/local/docker-compose.yml up -d --wait db
uv run alembic upgrade head
uv run python -m goldpulse.devserver
```

Open http://127.0.0.1:8000 and log in with `OPS_ACCESS_KEY`. On the first run the worker downloads the full
history, which takes a long time and needs about 1 GB of database space. For a quick try, set
`HISTORY_START_DATE` to a recent date. `make doctor` checks the environment variables and the connection to
each data source.

For a production-style deployment, use `deployment/production/docker-compose.yml` (db, migrate, web, worker,
backup). The optional BTC instance uses `deployment/btc/docker-compose.yml`. All variables are listed in
`deployment/.env.example`.

## Running tests

The tests run against a real PostgreSQL database (`goldpulse_test`, created automatically). Network calls are
replaced by fixtures and fakes.

```bash
make test
# or
docker compose -f deployment/local/docker-compose.yml up -d --wait db
uv run pytest -q
```

If your Postgres runs somewhere else, set `TEST_DATABASE_ADMIN_URL`. Run the linter with `make check` (Ruff).

## Project structure

```
src/goldpulse/
  api/            FastAPI routes (pages, JSON API, chat, BTC, health), auth deps, rate limiter
  brain/          LLM job consumer, prompts and JSON schemas
  eval/           replay tool for evaluating prompts and signals on historical data
  integrations/   Dukascopy, HistData, gold-api, FRED, Binance, Telegram, Claude backends
  repositories/   SQL access per aggregate
  services/       calendar, ingest, signals, model, predictions, paper trading, Fed, BTC strategies
  worker/         background loops (gold + BTC)
  web/            Jinja2 templates and static JS/CSS
migrations/       Alembic migrations
tests/            pytest suite (+ .bi5 fixtures)
data/             model coefficients and the static FOMC calendar used at runtime
research/         offline research scripts (feature engineering, model training, backtests)
deployment/       .env.example and Docker Compose files (local, production, BTC)
```
