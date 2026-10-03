"""Seed dogfood vòng 4: tua lại N ngày gần nhất của kho nến BTC bằng ĐÚNG logic app → tín hiệu + lệnh giấy BTC.

    DATABASE_URL=…/goldpulse_btcdev python -m uv run python deployment/local/seed_btc.py [số ngày, mặc định 7]

Với từng giờ tròn trong N ngày: tính 4 chiến thuật (`services/btc/strategies.evaluate`) trên 90 ngày nến trước đó rồi
`paper.close_open` / `paper.open_from_signal` với `now` = giờ đó + 61 giây — như worker chạy đúng giờ. TRUNCATE
inst_trade/inst_day/inst_signal trước khi tua (chạy lại được). PHẢI dừng worker trước (script tự kiểm). Chỉ dùng cho
DB dogfood: từ chối chạy trên DB có tên production (`goldpulse`, `goldpulse_btc`).
"""

import asyncio
import os
import sys
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import text

from goldpulse.db import engine, session_scope
from goldpulse.repositories import inst as repo
from goldpulse.services.btc import SYMBOL, paper, strategies

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
FORBIDDEN = {"goldpulse", "goldpulse_btc"}


async def main(days: int) -> None:
    db_name = urlsplit(os.environ.get("DATABASE_URL", "")).path.lstrip("/")
    if db_name in FORBIDDEN or not db_name:
        raise SystemExit(f"Từ chối: DB '{db_name}' là kho production — seed chỉ dùng cho DB dogfood")
    # Worker giữ advisory lock 20260924 suốt đời — còn worker thì seed và worker ghi chen nhau (dogfood vòng 4)
    lock = await engine.connect()
    if not (await lock.execute(text("SELECT pg_try_advisory_lock(20260924)"))).scalar():
        raise SystemExit("Còn một worker đang chạy trên DB này — dừng worker trước khi seed")
    async with session_scope() as s:
        first, last = await repo.candle_span(s, SYMBOL)
    if not first or not last or last - first < timedelta(days=days + 35):
        raise SystemExit("Kho nến BTC chưa đủ (cần ≥ số ngày tua + 35 ngày) — chạy worker BTC một lúc trước")
    end = last.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    async with session_scope() as s:
        await s.execute(text("TRUNCATE inst_trade, inst_day, inst_signal RESTART IDENTITY CASCADE"))
        await s.execute(text("UPDATE inst_account SET enabled = true"))
        bars = await repo.hourly_bars(s, SYMBOL, start - timedelta(days=95), end)
    sigs = {x["bar_ts"]: x for x in strategies.evaluate(bars)}
    opened = skipped = 0
    bar = start
    while bar < end:
        now = bar + timedelta(hours=1, seconds=61)
        async with session_scope() as s:
            await paper.close_open(s, now)
            sig = sigs.get(bar)
            entry = await repo.candle_at(s, SYMBOL, bar + timedelta(minutes=59))
            if sig is not None and entry is not None:
                await repo.insert_signal(s, SYMBOL, bar, now, sig["close"], sig["values"], sig["fired"], "seed")
                o, k = await paper.open_from_signal(s, sig, float(entry["close"]), now)
                opened, skipped = opened + o, skipped + k
        bar += timedelta(hours=1)
    async with session_scope() as s:
        await paper.close_open(s, end + timedelta(hours=1))
        for code in ("BTC-A", "BTC-B", "BTC-C", "BTC-D"):
            t = await repo.totals(s, code)
            print(
                f"{code}: {t['trades']} lệnh · cộng dồn {t['pnl']:+.2f} USD · cháy {t['burned_days']}/{t['days']} ngày"
            )
    print(f"Đã tua {days} ngày ({start:%d/%m} → {end:%d/%m %H:%M} UTC): mở {opened} lệnh, bỏ qua {skipped}")
    await lock.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
