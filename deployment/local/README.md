# deployment/local/

For local development, `docker-compose.yml` runs PostgreSQL 16 on `127.0.0.1:5432` (user and password
`goldpulse`, local use only). Copy `../.env.example` to `.env` in this folder and fill in the values (`.env` is
git-ignored). `make dev` starts the database, applies the migrations and runs web + worker + brain.

## Seed data

- `seed_paper.py` replays the last N days of the gold warehouse with the production logic to create
  predictions and paper trades. It refuses to run against a production database name.
- `seed_btc.py` does the same for the BTC instance.

## BTC dev instance

The BTC dev instance uses its own database, `goldpulse_btcdev`, on the same Postgres server. Create it once with
`CREATE DATABASE goldpulse_btcdev`. The key below is a local test value only.

```bash
export DATABASE_URL=postgresql+asyncpg://goldpulse:goldpulse@127.0.0.1:5432/goldpulse_btcdev INSTRUMENTS=BTCUSDT \
       OPS_ACCESS_KEY=btc-dev-key-0123456789 APP_ENV=development
uv run alembic upgrade head
uv run python -m goldpulse.worker                                   # terminal 1: loads 90 days of Binance candles
uv run uvicorn goldpulse.main:app --host 127.0.0.1 --port 8001      # terminal 2: http://127.0.0.1:8001
PYTHONPATH=src uv run python deployment/local/seed_btc.py 45        # optional; stop the worker first
```

To run gold and BTC together, set `INSTRUMENTS=XAUUSD,BTCUSDT`. A separate production stack for BTC is in
`deployment/btc/docker-compose.yml` (port 8081).
