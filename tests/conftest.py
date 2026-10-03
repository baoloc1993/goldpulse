"""Test dùng DB riêng `goldpulse_test` trên Postgres local (make test bật db trước)."""

import os
import subprocess
import sys

import pytest

BASE = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql://goldpulse:goldpulse@127.0.0.1:5432/goldpulse")
TEST_DB = "goldpulse_test"
os.environ["DATABASE_URL"] = BASE.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + "/" + TEST_DB
os.environ["OPS_ACCESS_KEY"] = "test-key-0123456789"
os.environ["APP_ENV"] = "test"
os.environ["INSTRUMENTS"] = (
    "XAUUSD,BTCUSDT"  # (v4) route /btc chỉ mount khi instance bật BTC  # cookie không Secure — client test đi http
)


def _db_available() -> bool:
    try:
        import psycopg2

        conn = psycopg2.connect(BASE, connect_timeout=3)
    except Exception:
        return False
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {TEST_DB}")
    conn.close()
    return True


DB_UP = _db_available()


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    if DB_UP:
        subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], check=True, capture_output=True)
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, capture_output=True)
    yield


needs_db = pytest.mark.skipif(not DB_UP, reason="Postgres local chưa chạy (make dev / docker compose)")
