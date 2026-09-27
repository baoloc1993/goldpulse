"""Seed dogfood vòng 3: tua lại N ngày gần nhất của kho bằng ĐÚNG logic production → dự đoán tự động + lệnh giấy.

    DATABASE_URL=…/goldpulse_dev python -m uv run python deployment/local/seed_paper.py [số ngày, mặc định 7]

Mỗi mốc 15 phút có trong kho: `model15m.evaluate` (mô hình thật) quyết có dự đoán không; có → ghi `prediction(auto,
backend='replay')` với giá `predictions.price_at` + `vol96`, rồi `paper.open_for_new` / `paper.close_due` bước từng
phút như worker. Chỉ dùng cho DB dogfood — từ chối chạy trên kho production `goldpulse`.
TRUNCATE paper_trade/paper_day + xoá prediction backend 'replay' trước khi tua (chạy lại được).
"""

import asyncio
import os
import sys
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import text

from goldpulse.config import settings
from goldpulse.db import engine, session_scope
from goldpulse.repositories import candles as candle_repo
from goldpulse.repositories import paper as paper_repo
from goldpulse.services import model15m, paper, predictions, signals

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]


async def main(days: int) -> None:
    # Danh sách TRẮNG (review v3): script TRUNCATE sổ giấy — lọt vào production là mất sổ 4 tuần của pha E
    db_name = urlsplit(os.environ.get("DATABASE_URL", "")).path.lstrip("/")
    if db_name not in ("goldpulse_dev", "goldpulse_dogfood") or settings.app_env == "production":
        raise SystemExit("Từ chối: seed chỉ chạy trên goldpulse_dev / goldpulse_dogfood, APP_ENV khác production.")
    async with session_scope() as s:
        await s.execute(text("TRUNCATE paper_trade, paper_day RESTART IDENTITY"))
        await s.execute(text("DELETE FROM prediction WHERE backend = 'replay'"))
        await s.execute(text("DELETE FROM worker_status WHERE key = 'paper'"))
        await s.execute(text("UPDATE paper_account SET enabled = true"))
        end = signals.bar_boundary(await candle_repo.latest_candle_ts(s))
    t = end - timedelta(days=days)
    made = 0
    while t <= end:
        async with session_scope() as s:
            if signals.auto_allowed(t):
                mdl = await model15m.evaluate(s, t)
                if mdl and mdl["trigger"]:
                    price = await predictions.price_at(s, t)
                    if price is not None:
                        slim = {k: v for k, v in mdl.items() if k != "features"}
                        sig = {"vol96": await signals.vol96(s, t), "model": slim}
                        await predictions.create(
                            s, made_at=t, source="auto", direction=mdl["direction"], confidence="low", price=price,
                            signals=sig, reasoning="(tua lại cho dogfood — không có lời của não)", backend="replay",
                            model="logit15m_v1", job_id=None,
                        )  # fmt: skip
                        made += 1
            await paper.open_for_new(s, t + timedelta(seconds=30))
        # bước từng phút khi còn lệnh mở, như score_loop của worker
        m = t + timedelta(minutes=1)
        while m < t + timedelta(minutes=15):
            async with session_scope() as s:
                if await paper_repo.open_trades(s):
                    await paper.close_due(s, m)
            m += timedelta(minutes=1)
        t += timedelta(minutes=15)
    async with session_scope() as s:
        await paper.close_due(s, end + timedelta(hours=2))
        rows = (await s.execute(text(
            "SELECT account_code, status, coalesce(exit_reason, skip_reason) AS why, count(*),"
            " round(sum(pnl)::numeric, 2)"
            " FROM paper_trade GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ))).all()  # fmt: skip
    print(f"{made} dự đoán tự động (mô hình thật) trong {days} ngày tới {end:%d/%m %H:%M} UTC")
    for r in rows:
        print("  ", *r)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
