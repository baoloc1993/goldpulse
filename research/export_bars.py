"""Xuất nến 15 phút gộp từ phút có giao dịch (cùng quy ước tín hiệu production) → CSV cho nghiên cứu."""

import asyncio
import csv
import sys
from datetime import UTC, datetime

from goldpulse.db import SessionLocal, engine
from goldpulse.repositories import candles as candle_repo

OUT = sys.argv[1]


async def main():
    start = datetime(2011, 11, 1, tzinfo=UTC)
    end = datetime(2026, 9, 26, tzinfo=UTC)
    async with SessionLocal() as s:
        bars = await candle_repo.live_bars(s, 15, end, start, 10**8)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "n"])
        for b in bars:
            w.writerow([b["ts"].isoformat(), b["open"], b["high"], b["low"], b["close"], b["n"]])
    print(len(bars), bars[0]["ts"], bars[-1]["ts"])
    await engine.dispose()


asyncio.run(main())
