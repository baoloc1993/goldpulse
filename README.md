# GoldPulse

[![CI](https://github.com/baoloc1993/goldpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/baoloc1993/goldpulse/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**A self-hosted market-data warehouse and signal pipeline for gold (XAUUSD), with an optional independent Bitcoin (BTCUSDT) instance.**
A background worker builds a 1-minute candle history from free public sources (2004 to today), keeps it current
with a realtime feed, computes 15-minute signals, records directional predictions that are scored automatically,
and feeds simulated ("paper") trading accounts. A password-protected FastAPI dashboard and a Telegram bot sit on top.

> **Not financial advice.** GoldPulse is a personal engineering and research project. Paper trading uses
> fake money, nothing here places real orders, and no figure in this repository is a promise of future
> performance. See [Honest limitations](#honest-limitations).

I built it to practise the unglamorous parts of backend work on a real, messy data problem: idempotent
ingestion from flaky third-party sources, long-running workers that survive outages, a relational schema that
evolves through migrations, and a web surface that is safe to expose to the internet.

**Size, from the code:** roughly 8,000 lines of Python in `src/goldpulse`, 5 Alembic migrations, 20 tables,
178 tests running against a real PostgreSQL.

## Table of contents

1. [The problem](#the-problem)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Engineering highlights](#engineering-highlights)
5. [Honest limitations](#honest-limitations)
6. [Data model](#data-model)
7. [HTTP routes](#http-routes)
8. [Tech stack](#tech-stack)
9. [Getting started](#getting-started)
10. [Testing](#testing)
11. [Project structure](#project-structure)
12. [Roadmap](#roadmap)
13. [Author](#author)

## The problem

Free gold data is scattered and uneven. Dukascopy publishes tick-derived history as binary `.bi5` files that are
slow and rate-limited. HistData.com publishes 1-minute CSV archives per year or month, about a week late. A
realtime quote API gives only the current price. None of them agree on time zones, holidays or gap handling, and each fails
in its own way (503s, hung connections, files that are not published yet).

GoldPulse turns them into one trustworthy store and answers questions on top of it:

- Is the history complete, and exactly which minutes are missing?
- Is the realtime feed alive, and how stale is it?
- Given the latest 15-minute bar, does a signal fire, and was the resulting prediction right?
- How would a simple rule have behaved on a small simulated account?

## Features

### Data ingestion

| Source | What it provides | Code |
|---|---|---|
| Dukascopy | `.bi5` files (LZMA-compressed binary, decoded to 1-minute candles) used for 2004 to 15 March 2009. Throttle handling, optional SOCKS proxy | `integrations/dukascopy.py` |
| HistData.com | 1-minute CSV archives from 15 March 2009 (one file per year, then per month), fetched through the site's public download form. New York time with DST is converted to UTC | `integrations/histdata.py` |
| gold-api.com | Realtime XAU price, polled every `REALTIME_POLL_SECONDS` (default 5 s) | `integrations/goldapi.py` |
| FRED | Federal Reserve target-rate series, joined with a static FOMC meeting calendar (`data/fomc_meetings.json`) | `integrations/fred.py`, `services/fed.py` |
| Binance (BTC instance) | Public spot REST klines, no API key | `integrations/binance.py` |

### Worker and backfill

- **History loop** keeps one row per UTC day in `crawl_day` and works through the `pending` ones. Old days that
  only Dukascopy can serve use a separate batch, so they never block the HistData queue.
- **Realtime loop** polls the price, stores every sample (with its lag), and builds a *provisional* 1-minute candle per
  minute, but only while the market is open (New York calendar with its daily break).
- **Backfill loop** (every `BACKFILL_HOURS`, default 24) re-downloads the current HistData month and overwrites the
  last 14 days of provisional candles with final data.
- **Session bars** (Asia / Europe / US) are recomputed on every write, with DST handled through `zoneinfo`.

### Monitoring web UI

Server-rendered Jinja2 pages with vanilla JS and CSS, no build step. The UI strings are in Vietnamese.

- **Overview**: ingestion progress, worker heartbeat, realtime lag against a threshold.
- **Coverage**: per-day coverage with the exact missing minute ranges, and a manual "retry" for failed days.
- **Query**: ad-hoc session statistics (by session, year range and weekday); query latency is recorded.
- **Predictions**, **FED calendar** (gold price reaction around each decision), **Chat**, **Paper accounts**, **BTC**.

### Prediction pipeline

- Three signals on 15-minute bars built from the live 1-minute candles: Bollinger %b(20, 2σ), a 6+ bar close streak and
  RSI(9) (`services/signals.py`). In the worker, the signals are recorded with each prediction and given to the brain as context; the model decides when a prediction fires.
- A pure-Python logistic-regression model (`services/model15m.py`, coefficients in `data/models/logit15m_v1.json`)
  that triggers only on its most confident 2% of bars. The production code is checked bar by bar against the pandas
  research implementation (`research/parity_model.py`).
- Predictions go into an append-only ledger, are scored once the horizon plus a grace period has passed
  (`correct` / `wrong` / `flat` / `unscored`), and are summarised with hit rates and a verdict rule declared in advance
  (`pending` / `go` / `pivot` / `kill`, see `services/predictions.py`).

### Paper trading

- Four XAUUSD accounts (A to D, seeded by migration 0004), each a different entry rule: every prediction, a
  volatility filter, a model-confidence filter, and a 60-minute horizon.
- Fake money only. Entries use the prediction's price, exits use the same price lookup that scores predictions,
  and each account pays a fixed spread. A day's loss is capped at the remaining equity, and an account that reaches zero is
  "burned" for the day. Everything is rows in Postgres: `services/paper.py` imports no exchange client.
- A unique constraint on `(account_code, prediction_id)` makes opening a trade idempotent.

### Chat assistant and Telegram

- A separate **brain** process consumes jobs from a Postgres queue (`FOR UPDATE SKIP LOCKED`). For each job it builds
  a fixed data pack from the warehouse (no tool use) and asks an LLM backend: the `claude` CLI, the Anthropic API,
  or a deterministic fake used in tests. The web app never calls an LLM, so it stays up when the brain is down.
- Safety rules live in code, not in the prompt: refusals, a disclaimer line appended to every answer, and a rule
  that drops any trade suggestion when the market is closed or the price feed is stale
  (`compose_answer` in `brain/__main__.py`, covered by `test_code_enforces_disclaimer_and_refusals_regardless_of_brain`).
- The worker pushes paper-trade entries and exits to **Telegram**. The notification cursor is advanced only after a
  successful send, so a failure leads to a retry rather than a lost message.

### BTC instance (optional)

A second, independent deployment (`INSTRUMENTS=BTCUSDT`, own database, compose project and Telegram bot):
Binance 1-minute klines, 1-hour features, a BTC logistic model (`data/models/btc_logit1h_v1.json`) and four paper strategies
(`model`, `model_trend`, `spike`, `squeeze`) on leveraged paper accounts. It does not run the chat brain.
One instance can also run both instruments with `INSTRUMENTS=XAUUSD,BTCUSDT`.

## Architecture

Three processes (web, worker, brain) share one PostgreSQL database and talk to each other only through it.

```mermaid
flowchart LR
    subgraph Sources
        DK[Dukascopy .bi5]
        HD[HistData CSV]
        GA[gold-api realtime]
        FR[FRED]
        BN[Binance klines]
    end

    subgraph Worker["worker (asyncio loops, advisory lock)"]
        H[history loop]
        R[realtime loop]
        B[backfill loop]
        S[signal loop]
        SC[score + paper loop]
        N[notify loop]
        F[fed loop]
        BT[BTC loops]
    end

    PG[(PostgreSQL 16)]
    WEB[web: FastAPI + Jinja2]
    BR[brain: LLM job consumer]
    LLM[LLM backend: claude CLI / Anthropic API / fake]
    TG[Telegram]
    U[Operator browser]

    DK --> H
    HD --> H
    HD --> B
    GA --> R
    FR --> F
    BN --> BT
    H --> PG
    R --> PG
    B --> PG
    S --> PG
    SC --> PG
    F --> PG
    BT --> PG
    PG --> N
    N --> TG
    PG <--> BR
    BR --> LLM
    U --> WEB
    WEB <--> PG
```

The code is layered: `integrations/` (external HTTP clients, no database) then `repositories/` (SQL) then
`services/` (domain logic, no HTTP) then `api/` and `worker/`. A service never imports an HTTP client, which keeps the
domain logic testable with plain data.

### From a price tick to a notification

```mermaid
sequenceDiagram
    participant S as Sources
    participant W as Worker
    participant DB as PostgreSQL
    participant BR as Brain
    participant T as Telegram

    S->>W: realtime quote (every ~5 s)
    W->>DB: feed_sample + provisional 1-min candle (market open only)
    Note over W,DB: Daily backfill replaces provisional candles with final ones
    W->>DB: signal loop: 15-min mark + 60 s, compute signals and model
    alt model triggers and market open for the next 15 min
        W->>DB: enqueue signal_prediction (unique per bar_ts)
        BR->>DB: claim job (SKIP LOCKED)
        BR->>DB: write prediction (append-only)
    end
    W->>DB: score due predictions, close due paper trades
    W->>DB: open paper trades for new predictions (4 rules)
    W->>DB: notify loop reads the paper ledger
    W->>T: "opened / closed" message
    W->>DB: move the cursor only after a successful send
```

## Engineering highlights

**Data warehouse and migrations.** `candle_1m` is keyed by `ts timestamptz`, `session_bar` by
`(session, trade_date)`, with ingestion-state and ledger tables around them. Five Alembic migrations; the paper and BTC
ones only add tables and seed rows. The test session runs `alembic downgrade base` then
`upgrade head` against a real database, so a broken migration fails the suite.

**Idempotent ingestion.** Every write is an upsert, and precedence between sources is encoded in SQL
(`repositories/candles.py`): a provisional realtime candle never overwrites a final one; a HistData candle overwrites provisional
and older HistData rows but not Dukascopy rows; Dukascopy overwrites everything. Bulk upserts use one `unnest()` statement
instead of row loops. Re-running a day is therefore safe, which tests such as `test_retry_error_day_idempotent`
and `test_backfill_overwrites_temp_keeps_dukascopy_and_never_touches_crawl_day` check.

**Gap and coverage tracking.** `crawl_day` stores, for each UTC day, the status
(`pending | ok | empty | error`), the number of open-market minutes from the calendar, how many were covered, and the missing ranges as
merged `[from, to]` intervals in JSONB. Weekends and holidays are classified `empty` and excluded from the denominator, and
days with no open market minutes (UTC Saturdays) are never downloaded. Transient failures keep the day `pending` with a back-off; only after
`MAX_ATTEMPTS` (5) does it become `error`. Days whose source file is not published yet do not block the coverage verdict
(`test_edge_days_awaiting_release_do_not_block_coverage_verdict`).

**Scheduling and worker resilience.**
- A Postgres advisory lock (`pg_try_advisory_lock`) held for the worker's lifetime means a second worker exits
  immediately instead of double-counting retry attempts.
- Each loop runs under a supervisor that logs, records the error and restarts the loop after 10 s, so one failing
  loop does not take the process down. Sleeps are interruptible (graceful stop, or the operator's "retry now").
- A fully unreachable source pauses the queue; a throttling source backs off exponentially from 60 s up to
  900 s; isolated failures only put that one day to sleep.
- The brain process guards against a silently dead database connection (after a Docker/WSL restart an `asyncpg`
  connection can hang with no error). Job claiming and processing run under timeouts (30 s and 300 s); on expiry it invalidates the lock
  connection, disposes the pool, reconnects and re-takes its advisory lock (`reconnect()` in `brain/__main__.py`,
  tested by `test_brain_recovers_when_db_hangs`). A comment in the code records the incident that motivated it.
- The score loop requeues jobs stuck in `running`, expires chat questions that wait more than 10 minutes and drops stale
  automatic prediction jobs. It runs in the worker precisely because the brain may be down.

**Security hardening of the web app.**
- One operator key (minimum 16 characters, enforced at startup). The cookie holds an HMAC-derived token, never the key;
  comparisons use `hmac.compare_digest`; the cookie is `HttpOnly`, `SameSite=Strict` and `Secure` by default outside development.
- Login is limited to 10 attempts per minute per client IP (SlowAPI); other mutating endpoints have their own limits.
  The `next=` redirect target must be an internal path (`test_login_next_only_internal`).
- `TRUST_CLOUDFLARE_IP` is **opt-in**. When enabled, `CF-Connecting-IP` is trusted only if the TCP peer is loopback or private
  (the tunnel on the same host), and uvicorn runs with `--no-proxy-headers`, so a caller cannot forge their address to dodge the
  limiter (`test_client_ip_trusts_cloudflare_header_only_from_internal_peer`).
- Response headers: a strict Content-Security-Policy (`default-src 'self'`, no inline script or style, `frame-ancestors 'none'`),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` on dynamic pages. OpenAPI docs are disabled.
- A 64 KB request-body limit enforced at the ASGI layer (both `Content-Length` and chunked), a request id on every
  log line, and an exception handler that logs the context but returns only a generic message, never a traceback.
- There is no CSRF token: mutating endpoints rely on `SameSite=Strict` plus JSON bodies. That is a deliberate trade-off for a
  single-operator tool and is listed under limitations.

**Docker deployment.** One image for web and worker (`python:3.12-slim`, non-root user, `uv sync --frozen`). The production compose stack
runs `db` (with a healthcheck), a one-shot `migrate` service, then `web` (HTTP healthcheck on `/health`) and `worker`, plus a `backup` service that runs
`pg_dump -Fc` daily and keeps 14 dumps. The BTC instance has its own compose project, so the two stacks share nothing.

**Doctor and health checks.** `make doctor` validates required variables, flags variables that are not declared in
`.env.example`, then probes Postgres, gold-api, HistData, Dukascopy, FRED, Binance and the LLM backend's login state
without spending credits. `/health` is public but returns only `{"ok": true|false}` (503 when the database is unreachable).

**Replay and evaluation tooling.** `goldpulse.eval.replay` replays history to test the prediction prompt and signal rules.
It removes look-ahead (no dates, no FED calendar, prices shifted by a random offset), scores exactly as production does, compares against a
free baseline ("follow the signal hint, no LLM call") and only reads the warehouse. `deployment/local/seed_paper.py` replays the last N days through the real production
functions to populate a development database, and refuses to run against a database that is not on its allow-list.

**Tests against a real Postgres.** 178 tests, no mocked database. They cover the Dukascopy decoder against real `.bi5`
fixtures, HistData parsing, the New York calendar and DST, ingestion flows, coverage maths, the access matrix of every
route, rate limiting, security headers, the chat round trip with the fake brain, queue ordering, scoring, paper-account
rules and the BTC flows.

## Honest limitations

- **No evidence of a trading edge.** The model file records its own evaluation: trained on 2012-2021, the confidence
  threshold chosen on 2022-2024, then tested on 2025-2026. The committed metrics are:

  | Split (from `data/models/logit15m_v1.json`) | Accuracy | Predictions | Share of bars traded |
  |---|---|---|---|
  | train 2012-2021 | 58.81% | 3,938 | 1.73% |
  | threshold selection 2022-2024 | 56.73% | 1,375 | 2.0% |
  | hold-out 2025-2026 | 56.7% | 977 | 2.44% |

  These are direction hit rates on one historical window, **before** spread, slippage, execution delay or any
  live-trading effect. Treat them as evidence that the pipeline works end to end, not as a strategy. Live
  predictions are scored in the app, whose own verdict rule requires a minimum sample before it concludes anything.
- **Paper trading is simplistic.** Fixed spread, no slippage model, no partial fills, prices from free feeds.
- **Free-data fragility.** HistData is fetched through its public download form and could change or block;
  Dukascopy history is slow and sometimes needs a proxy; the realtime feed has some seconds of lag. The code defends
  against all of this, but it depends on sources it does not control.
- **Single operator, single worker.** One shared access key, no user accounts, no roles. One worker per database by design
  (advisory lock); the worker is not horizontally scalable.
- **Web hardening has a scope.** No CSRF tokens (see above), and TLS is expected to be terminated in front of the app
  (for example by a tunnel); the app itself serves plain HTTP.
- **Vietnamese UI and code comments.** The interface text and many code comments are in Vietnamese, because it is a personal tool
  for a Vietnamese-speaking operator. Identifiers, schemas and this README are in English.
- **Research scripts are not reproducible from this repo alone.** `research/` needs numpy, pandas and scikit-learn
  (not project dependencies) and a CSV export (about 23 MB, not committed).
- **LLM output is advisory and optional.** The `claude` CLI backend needs a local login on the host
  that runs it, so the brain runs outside the Docker image.
- **CI is new.** The GitHub Actions workflow mirrors `make check` and `make test`, which I ran locally (ruff clean, 178 passed on a throwaway `postgres:16-alpine`).

## Data model

All timestamps are UTC `timestamptz`. Tables are created by migrations `0001` to `0005` (`migrations/versions/`);
the models are in `src/goldpulse/models/__init__.py`.

| Area | Table | Purpose |
|---|---|---|
| Warehouse | `candle_1m` | 1-minute OHLCV with `source` (`dukascopy` / `histdata` / `goldapi`) and `is_final` |
| | `session_bar` | Asia / Europe / US session OHLC per trade date, minutes present vs traded |
| Ingestion state | `crawl_day` | per-day status, attempts, last error, coverage counters and missing ranges (JSONB) |
| | `feed_sample` | every realtime poll with source timestamp, lag and ok flag |
| | `worker_status` | key/JSONB state: heartbeat, source health, last signal, backfill, Telegram cursor |
| | `query_log` | ad-hoc query parameters and duration, for the latency metric |
| Prediction | `bot_job` | Postgres job queue (kind, payload, status, attempts, result) |
| | `prediction` | the scoring ledger: direction, confidence, prices, horizon, outcome, signals snapshot |
| | `fed_decision` | one row per FOMC meeting: rate range, change in bp, scheduled or not |
| Chat | `chat_thread`, `chat_message` | conversations, with job link, context pack and usage |
| Paper (XAUUSD) | `paper_account` | four rule-based accounts, seeded by migration |
| | `paper_trade` | one row per (account, prediction); status and skip reason |
| | `paper_day` | daily equity ledger and "burned" flag |
| BTC | `inst_candle`, `inst_feed`, `inst_signal` | candles, feed samples and hourly signals for BTCUSDT |
| | `inst_account`, `inst_trade`, `inst_day` | four strategy accounts, their trades and daily ledger |

## HTTP routes

Everything except `/health`, `/login` and `/static/*` requires the operator cookie (pages redirect to `/login`, the API returns 401).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Public liveness: `{"ok": bool}`, 503 if the database is down |
| GET, POST | `/login` | Key form; POST limited to 10/min per IP |
| GET | `/overview`, `/coverage`, `/query` | Monitoring pages |
| GET | `/chat`, `/predictions`, `/fed` | Assistant, prediction ledger, FED calendar pages |
| GET | `/paper`, `/paper/{code}` | Paper-account pages |
| GET | `/btc`, `/btc/{code}` | BTC pages (only when `INSTRUMENTS` includes BTCUSDT) |
| GET | `/api/overview`, `/api/coverage` | Ingestion and coverage data |
| GET | `/api/query` | Session statistics (`session`, `year_from`, `year_to`, optional `dow`) |
| POST | `/api/crawl-days/{day}/retry` | Re-queue a failed day (30/min) |
| POST | `/api/history/wake` | Wake the history loop now (10/min) |
| GET | `/api/chat/header`, `/api/chat/thread`, `/api/chat/messages/{id}` | Chat state and polling |
| POST | `/api/chat/messages` (202), `/api/chat/messages/{id}/resend` (202), `/api/chat/threads` (201) | Ask, resend, new thread (20/min) |
| GET | `/api/predictions`, `/api/predictions/rows`, `/api/fed` | Ledger summary, rows, FED data |
| GET | `/api/paper/accounts`, `/api/paper/accounts/{code}`, `/api/paper/accounts/{code}/trades` | Paper accounts and trade log |
| POST | `/api/paper/accounts/{code}/enabled` | Enable or disable an account (30/min) |
| GET, POST | `/api/btc/accounts`, `/api/btc/accounts/{code}`, `/api/btc/accounts/{code}/trades`, `/api/btc/accounts/{code}/enabled` | Same, for BTC |

OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) are turned off.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Web | FastAPI, Uvicorn, Jinja2, vanilla JS and CSS, SlowAPI |
| Database | PostgreSQL 16, SQLAlchemy / SQLModel (async, asyncpg), Alembic (psycopg2 for migrations) |
| HTTP clients | httpx (with SOCKS support) |
| Config and logging | pydantic-settings, structlog (JSON in production), optional Sentry |
| LLM (optional) | `claude` CLI, Anthropic SDK, or a deterministic fake |
| Tooling | uv, Ruff (lint and format), pytest, pytest-asyncio |
| Deployment | Docker, Docker Compose, GitHub Actions CI |

## Getting started

**Prerequisites:** [uv](https://docs.astral.sh/uv/), Docker (for PostgreSQL) and, optionally, `make`.

```bash
git clone https://github.com/baoloc1993/goldpulse.git
cd goldpulse
cp deployment/.env.example deployment/local/.env
# edit deployment/local/.env: DATABASE_URL, OPS_ACCESS_KEY, BRAIN_BACKEND (see the table below)

uv sync
make dev        # Postgres (docker), migrations, then web (:8000) + worker + brain
```

Without `make`:

```bash
docker compose -f deployment/local/docker-compose.yml up -d --wait db
uv run alembic upgrade head
uv run python -m goldpulse.devserver
```

Open <http://127.0.0.1:8000> and sign in with `OPS_ACCESS_KEY`. A minimal local `.env`:

```env
DATABASE_URL=postgresql+asyncpg://goldpulse:goldpulse@127.0.0.1:5432/goldpulse
OPS_ACCESS_KEY=<any random string of 16+ characters>
BRAIN_BACKEND=fake
```

The local compose file uses the throwaway credentials `goldpulse` / `goldpulse` on `127.0.0.1` only. The first run downloads the
full history (about 1 GB of database space, a long time); for a quick look set `HISTORY_START_DATE` to a recent date.
Two workers must never write to the same database, so stop `make dev` before deploying to the same volume.

### Environment variables

Declared in `deployment/.env.example` (names and comments only, never values; `.env` files are git-ignored).
The comments in that file are in Vietnamese; this table is the English reference.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL` | yes | | `postgresql+asyncpg://user:pass@host:5432/db` |
| `OPS_ACCESS_KEY` | yes | | Operator key, at least 16 characters |
| `APP_ENV` | no | `development` | `development` / `production` (production: Secure cookie, JSON logs, `fake` brain refused) |
| `POSTGRES_PASSWORD` | production | | Password for the compose `db` container |
| `HISTORY_START_DATE` | no | `2004-01-01` | First day to load |
| `REALTIME_POLL_SECONDS` | no | `5` | gold-api polling interval |
| `BACKFILL_HOURS` | no | `24` | HistData backfill interval |
| `DUKASCOPY_CONCURRENCY`, `DUKASCOPY_DELAY_SECONDS` | no | `3`, `0.5` | Parallel downloads and pause per file |
| `DUKASCOPY_PROXY` | no | | Proxy used only for Dukascopy, e.g. `socks5h://127.0.0.1:40000` |
| `WEB_BIND` | production | `127.0.0.1` | Host interface for the published web port |
| `COOKIE_SECURE` | no | by `APP_ENV` | Set `false` when browsing over plain HTTP on a LAN |
| `TRUST_CLOUDFLARE_IP` | no | `false` | Only behind a Cloudflare Tunnel |
| `BRAIN_BACKEND` | no | `cli` | `cli` (the `claude` command) / `api` / `fake` (tests only) |
| `CLAUDE_CLI_MODEL`, `CLAUDE_CLI_EFFORT`, `CLAUDE_CLI_BIN` | no | `opus`, `medium`, `claude` | CLI backend settings |
| `ANTHROPIC_API_KEY`, `CLAUDE_API_MODEL` | with `api` | | API backend |
| `BRAIN_TIMEOUT_SECONDS` | no | `120` | Timeout of one LLM call |
| `PREDICTION_HORIZON_MINUTES` | no | `15` | Scoring horizon |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | no | off | Paper-trade notifications (the chat id is auto-detected when empty) |
| `SENTRY_DSN` | no | | Error tracking |
| `INSTRUMENTS` | no | `XAUUSD` | `XAUUSD`, `BTCUSDT` or both (comma-separated) |
| `BINANCE_BASE_URL`, `BTC_POLL_SECONDS` | no | `https://api.binance.com`, `10` | BTC feed |
| `TELEGRAM_BTC_BOT_TOKEN`, `TELEGRAM_BTC_CHAT_ID` | no | off | Separate Telegram bot for BTC |

### Make targets

| Target | What it does |
|---|---|
| `make help` | List targets |
| `make db` | Start local PostgreSQL 16 (docker compose) |
| `make dev` | `db`, then web + worker + brain via `goldpulse.devserver` |
| `make check` | `ruff check`, `ruff format --check`, import smoke test |
| `make test` | Start the DB, then `pytest -q` |
| `make migrate` | `alembic upgrade head` |
| `make doctor` | Check env vars and connectivity |
| `make brain` | Run the brain on the host (`--production` refuses the fake backend) |
| `make deploy` | `check` + `test`, then build and start the production compose stack |

### Docker / production

```bash
cp deployment/.env.example deployment/production/.env   # fill in POSTGRES_PASSWORD, DATABASE_URL (host: db), OPS_ACCESS_KEY, ...
make deploy
# equivalent to: docker compose -f deployment/production/docker-compose.yml --env-file deployment/production/.env up -d --build
```

The stack contains `db`, a one-shot `migrate`, `web` (published on port 8080), `worker` and a daily `backup`. Run the brain
with `make brain` on the host (the `cli` backend uses the local `claude` login). The BTC instance:

```bash
cp deployment/btc/.env.example deployment/btc/.env
docker compose -f deployment/btc/docker-compose.yml --env-file deployment/btc/.env up -d --build   # web on :8081
```

### Seeding paper accounts

The four XAUUSD accounts and the four BTC accounts are created by the migrations, so `make migrate` is enough. To fill a **development**
database with sample predictions and paper trades, replay recent history through the production logic:

```bash
# needs a warehouse that already has data; the script only accepts the databases goldpulse_dev / goldpulse_dogfood
DATABASE_URL=postgresql+asyncpg://goldpulse:goldpulse@127.0.0.1:5432/goldpulse_dev \
  PYTHONPATH=src uv run python deployment/local/seed_paper.py 7     # last 7 days
```

`deployment/local/seed_btc.py` does the same for BTC; see `deployment/local/README.md`.

## Testing

```bash
make test
# or
docker compose -f deployment/local/docker-compose.yml up -d --wait db
uv run pytest -q
```

There are **178 tests**. They talk to a real PostgreSQL: `tests/conftest.py` creates a `goldpulse_test` database, runs
`alembic downgrade base` and `upgrade head`, and the tests use that. HTTP calls to data sources are replaced by fixtures
(real `.bi5` samples in `tests/fixtures/`) and fakes. If your Postgres is elsewhere, set `TEST_DATABASE_ADMIN_URL`
(default `postgresql://goldpulse:goldpulse@127.0.0.1:5432/goldpulse`). Without a reachable database the DB tests are skipped,
not failed. The same lint and test steps run in GitHub Actions (`.github/workflows/ci.yml`) against a `postgres:16-alpine` service.

## Project structure

```
src/goldpulse/
  api/            FastAPI routes (pages, JSON API, chat, BTC, health), auth dependencies, rate limiter
  brain/          LLM job consumer, prompts and JSON schemas
  eval/           replay tool for evaluating prompts and signals on historical data
  integrations/   Dukascopy, HistData, gold-api, FRED, Binance, Telegram, LLM backends
  models/         SQLModel table definitions
  repositories/   SQL access per aggregate
  services/       calendar, ingest, signals, model, predictions, paper trading, FED, notifications, BTC (services/btc)
  worker/         background loops (gold, plus btc.py)
  web/            Jinja2 templates and static JS/CSS
  config.py, db.py, doctor.py, devserver.py, logs.py, main.py
migrations/       Alembic migrations 0001-0005
tests/            pytest suite (+ .bi5 fixtures)
data/             model coefficients (data/models) and the static FOMC calendar
research/         offline research scripts (features, model training, backtests); not part of the app
deployment/       .env.example, Docker Compose files (local, production, btc), seed scripts
.github/workflows/ci.yml
```

## Roadmap

- Replace the single access key with per-user sessions, and add CSRF tokens.
- Export metrics (ingest lag, queue depth) next to the existing structured logs.
- English UI strings and comments.

## Author

**Ngo Le Bao Loc**, backend developer.

- GitHub: [baoloc1993](https://github.com/baoloc1993)
- Email: baoloc1993@gmail.com

Licensed under the [MIT License](LICENSE).
