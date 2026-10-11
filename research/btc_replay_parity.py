"""Tua lại N ngày gần nhất bằng CHÍNH code app (services/btc/paper.py trên DB `goldpulse_test`) và bằng
`btc_strategies.simulate` (nghiên cứu) trên cùng nến 1m Binance → so danh sách lệnh từng tài khoản.

Ghi đè `goldpulse_test` (bảng inst_*) — DB của `make test`, không phải production.

    PYTHONPATH=src python -m uv run --no-project --with numpy --with pandas --with scikit-learn \
        --with "sqlalchemy[asyncio]" --with asyncpg --with pydantic-settings --with structlog --with httpx         python research/btc_replay_parity.py [ngày=30]
"""

import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

os.environ["DATABASE_URL"] = "postgresql+asyncpg://goldpulse:goldpulse@127.0.0.1:5432/goldpulse_test"
os.environ.setdefault("OPS_ACCESS_KEY", "replay-key-0123456789")
os.environ["APP_ENV"] = "test"

import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "src"))
from btc_fetch import BASE, fetch  # noqa: E402
from btc_strategies import build_variants, load, simulate  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
PICK = {
    "BTC-A": ("mô hình", "top 5% giữ 1h"),
    "BTC-B": ("mô hình + xu hướng", "top 10% cùng chiều EMA 24h, giữ 1h"),
    "BTC-C": ("đánh theo nến giật", "|Δ1h| ≥ 3.0σ, giữ 4h"),
    "BTC-D": ("nén rồi bung", "Bollinger hẹp 20% thấp nhất 30 ngày, giữ 4h"),
}


def load_1m(first: date, last: date) -> pd.DataFrame:
    parts = []
    m = first.replace(day=1)
    while m <= last:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        df = fetch(f"{BASE}/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{m:%Y-%m}.zip")
        if df is None:
            d = m
            while d < min(nxt, last + timedelta(days=1)):
                x = fetch(f"{BASE}/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{d:%Y-%m-%d}.zip")
                if x is not None:
                    parts.append(x)
                d += timedelta(days=1)
        else:
            parts.append(df)
        m = nxt
    one = pd.concat(parts).drop_duplicates("ts").sort_values("ts")
    lo = pd.Timestamp(first, tz="UTC")
    hi = pd.Timestamp(last + timedelta(days=1), tz="UTC")
    return one[(one.ts >= lo) & (one.ts < hi)].reset_index(drop=True)


def research_trades(start: date) -> dict[str, list[tuple]]:
    """Bản nghiên cứu nguyên vẹn: lịch sử đầy đủ `btc_bars15.csv` (cùng nguồn nến Binance), mô hình luyện 2018–21."""
    b15, h1, e15, end_idx = load()
    V, sigma = build_variants(h1)
    out = {}
    for code, fam in PICK.items():
        v = next(x for x in V if (x.family, x.name) == fam)
        t = simulate(v, b15, h1, e15, end_idx, sigma)
        t = t[pd.to_datetime(t["day"]).dt.date >= start]
        out[code] = [(pd.Timestamp(r.day).date(), round(r.pnl, 3), bool(r.burn)) for r in t.itertuples()]
    return out


async def app_trades(one: pd.DataFrame, start: date, end: date) -> dict[str, list[tuple]]:
    from sqlalchemy import text

    from goldpulse.db import engine, session_scope
    from goldpulse.repositories import inst as repo
    from goldpulse.services.btc import SYMBOL, paper, strategies

    async with session_scope() as s:
        await s.execute(
            text("TRUNCATE inst_trade, inst_day, inst_signal, inst_candle, inst_feed RESTART IDENTITY CASCADE")
        )
        await s.execute(text("UPDATE inst_account SET enabled = true"))
    rows = [(r.ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume) for r in one.itertuples()]
    for i in range(0, len(rows), 20000):
        async with session_scope() as s:
            await repo.upsert_candles(s, SYMBOL, rows[i : i + 20000])
    t0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    t1 = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    async with session_scope() as s:
        bars = await repo.hourly_bars(s, SYMBOL, t0 - timedelta(days=95), t1)
    sigs = {x["bar_ts"]: x for x in strategies.evaluate(bars)}
    closes = {r[0]: r[4] for r in rows}
    bar = t0
    while bar < t1:
        now = bar + timedelta(hours=1, seconds=60)
        async with session_scope() as s:
            await paper.close_open(s, now)
            sig = sigs.get(bar)
            entry = closes.get(bar + timedelta(minutes=59))
            if sig is not None and entry is not None:
                await paper.open_from_signal(s, sig, float(entry), now)
        bar += timedelta(hours=1)
    async with session_scope() as s:
        await paper.close_open(s, t1 + timedelta(hours=1))
        out = {}
        for code in PICK:
            ts = [t for t in reversed(await repo.trades_page(s, code, None, 10000)) if t["status"] == "closed"]
            out[code] = [(t["trade_date"], round(t["pnl"], 3), t["exit_reason"] == "stopout") for t in ts]
    await engine.dispose()
    return out


def main(days: int = 30) -> int:
    end = pd.read_csv(Path(__file__).with_name("btc_bars15.csv"), usecols=["ts"]).ts.iloc[-1][:10]
    end = date.fromisoformat(end)  # ngày cuối của file nghiên cứu — hai bên cùng khoảng
    start = end - timedelta(days=days - 1)
    one = load_1m(start - timedelta(days=100), end)
    print(f"{len(one)} nến 1m · {one.ts.iloc[0]} → {one.ts.iloc[-1]} · so {start} → {end}")
    ref = research_trades(start)
    got = asyncio.run(app_trades(one, start, end))
    bad = 0
    for code in PICK:
        a, b = got[code], ref[code]
        same = len(a) == len(b) and all(
            x[0] == y[0] and abs(x[1] - y[1]) <= 0.002 and x[2] == y[2] for x, y in zip(a, b, strict=False)
        )
        bad += not same
        print(
            f"{code}: app {len(a)} lệnh {sum(x[1] for x in a):+.2f} USD · nghiên cứu {len(b)} lệnh"
            f" {sum(y[1] for y in b):+.2f} USD · {'KHỚP' if same else 'LỆCH'}"
        )
        if not same:
            for i, (x, y) in enumerate(zip(a, b, strict=False)):
                if x[0] != y[0] or abs(x[1] - y[1]) > 0.002 or x[2] != y[2]:
                    print("   đầu tiên lệch ở lệnh", i, "app", x, "nghiên cứu", y)
                    break
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
