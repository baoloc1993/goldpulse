"""crawl_day — hàng chờ nạp lịch sử + nguồn số liệu độ phủ."""

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_ATTEMPTS = 5


async def existing_last_day(session: AsyncSession) -> date | None:
    return (await session.execute(text("SELECT max(day) FROM crawl_day"))).scalar()


async def insert_pending(session: AsyncSession, rows: Sequence[tuple[date, int]]) -> None:
    """rows = (ngày, số phút thị trường mở). Ngày đã có thì giữ nguyên.

    Ngày không có phút mở nào (thứ Bảy UTC) → `empty` luôn, không tải: file Dukascopy của ngày đó
    chỉ có nến đứng giá (đo thật 2026-09-24) và Dukascopy chậm 15–50 giây mỗi file.
    """
    if not rows:
        return
    await session.execute(
        text("""
            INSERT INTO crawl_day (day, status, calendar_minutes)
            SELECT d, CASE WHEN m = 0 THEN 'empty' ELSE 'pending' END, m
            FROM unnest(CAST(:d AS date[]), CAST(:m AS int[])) AS t(d, m)
            ON CONFLICT (day) DO NOTHING
        """),
        {"d": [r[0] for r in rows], "m": [r[1] for r in rows]},
    )


async def next_batch(
    session: AsyncSession, limit: int, before: date | None = None, since: date | None = None
) -> list[date]:
    """Ngày pending (lần đầu, hoặc đã nghỉ ≥ 2 phút sau lỗi chập chờn), hoặc error còn lượt
    thử và đã nghỉ ≥ 10 phút — cũ nhất trước."""
    res = await session.execute(
        text("""
            SELECT day FROM crawl_day
            WHERE ((status = 'pending' AND (attempts = 0 OR fetched_at < now() - interval '2 minutes'))
               OR (status = 'error' AND attempts < :max
                   AND (fetched_at IS NULL OR fetched_at < now() - interval '10 minutes'))
            ) AND day < :before AND day >= :since
            ORDER BY day LIMIT :limit
        """),
        {"max": MAX_ATTEMPTS, "limit": limit, "before": before or date.max, "since": since or date.min},
    )
    return [r[0] for r in res]


async def mark_done(
    session: AsyncSession,
    day: date,
    status: str,
    candles: int,
    traded: int,
    covered: int,
    missing: list[list[str]],
) -> None:
    await session.execute(
        text("""
            UPDATE crawl_day SET status = :status, candles = :candles, traded_minutes = :traded,
                   covered_minutes = :covered, missing_ranges = CAST(:missing AS jsonb),
                   last_error = NULL, fetched_at = now()
            WHERE day = :day
        """),
        {
            "day": day,
            "status": status,
            "candles": candles,
            "traded": traded,
            "covered": covered,
            "missing": json.dumps(missing),
        },
    )


async def mark_error(session: AsyncSession, day: date, message: str) -> None:
    await session.execute(
        text("""
            UPDATE crawl_day SET status = 'error', attempts = attempts + 1,
                   last_error = :msg, fetched_at = now()
            WHERE day = :day
        """),
        {"day": day, "msg": message[:500]},
    )


async def mark_soft_fail(session: AsyncSession, day: date, message: str) -> None:
    """Lỗi chập chờn (429/5xx/treo): giữ pending để thử lại; quá MAX_ATTEMPTS lần mới thành error."""
    await session.execute(
        text("""
            UPDATE crawl_day SET attempts = attempts + 1,
                   status = CASE WHEN attempts + 1 >= :max THEN 'error' ELSE 'pending' END,
                   last_error = :msg, fetched_at = now()
            WHERE day = :day
        """),
        {"day": day, "msg": message[:500], "max": MAX_ATTEMPTS},
    )


async def retry(session: AsyncSession, day: date) -> bool:
    """Đưa ngày lỗi về pending + attempts = 0. Bấm hai lần vẫn một kết quả (idempotent)."""
    res = await session.execute(
        text("""
            UPDATE crawl_day SET status = 'pending', attempts = 0
            WHERE day = :day AND status IN ('error', 'pending')
        """),
        {"day": day},
    )
    return res.rowcount > 0  # type: ignore[attr-defined]


# Ngày "chờ phát hành": pending, chưa thử lần nào, nằm SAU ngày có phút mở cuối cùng đã xử lý (ok / empty /
# error) — nguồn (HistData trễ ~1 tuần) chưa có chứ không phải thiếu. Pending nằm giữa các ngày đã xử lý là
# thiếu thật. Không tính vào mẫu số độ phủ, không làm S1/S2 kẹt "đang nạp" mãi.
_AWAITING = """
    (status = 'pending' AND attempts = 0
     AND day > (SELECT coalesce(max(day), DATE '1900-01-01') FROM crawl_day
                WHERE status <> 'pending' AND calendar_minutes > 0))
"""


async def progress(session: AsyncSession) -> dict[str, int]:
    """Đếm theo trạng thái. Thứ Bảy (không tải) → `skipped`; ngày mép nguồn chưa phát hành → `awaiting`."""
    res = await session.execute(
        text(f"""
            SELECT CASE WHEN calendar_minutes = 0 THEN 'skipped'
                        WHEN {_AWAITING} THEN 'awaiting'
                        ELSE status END, count(*)
            FROM crawl_day GROUP BY 1
        """)
    )
    counts = {"pending": 0, "ok": 0, "empty": 0, "error": 0, "skipped": 0, "awaiting": 0}
    counts.update({r[0]: r[1] for r in res})
    return counts


async def coverage_by_year(session: AsyncSession) -> list[dict[str, Any]]:
    """Độ phủ theo năm. Ngày `empty` (lễ) bị loại khỏi mẫu số; pending/error tính là thiếu."""
    res = await session.execute(
        text(f"""
            SELECT extract(year FROM day)::int AS year,
                   sum(calendar_minutes) FILTER (WHERE status <> 'empty' AND NOT {_AWAITING}) AS calendar,
                   sum(covered_minutes) FILTER (WHERE status = 'ok') AS covered,
                   sum(traded_minutes) FILTER (WHERE status = 'ok') AS traded,
                   count(*) FILTER (WHERE calendar_minutes > 0 AND NOT {_AWAITING}) AS days,
                   count(*) FILTER (WHERE calendar_minutes > 0 AND status IN ('ok', 'empty')) AS done_days,
                   count(*) FILTER (WHERE status = 'pending' AND NOT {_AWAITING}) AS pending_days,
                   count(*) FILTER (WHERE status = 'error') AS error_days
            FROM crawl_day GROUP BY 1 ORDER BY 1 DESC
        """)
    )
    return [dict(r) for r in res.mappings()]


async def gaps_of_year(session: AsyncSession, year: int) -> list[dict[str, Any]]:
    """Mọi ngày có phút thị trường mở trong năm, theo thứ tự — service tự gộp đoạn thiếu."""
    res = await session.execute(
        text("""
            SELECT day, status, missing_ranges, calendar_minutes FROM crawl_day
            WHERE day >= make_date(:y, 1, 1) AND day < make_date(:y + 1, 1, 1)
              AND calendar_minutes > 0 AND status <> 'empty'
            ORDER BY day
        """),
        {"y": year},
    )
    return [dict(r) for r in res.mappings()]


async def error_days(session: AsyncSession) -> list[dict[str, Any]]:
    res = await session.execute(
        text("""
            SELECT day, last_error, attempts FROM crawl_day
            WHERE status = 'error' ORDER BY day
        """)
    )
    return [dict(r) for r in res.mappings()]


async def total_days(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT count(*) FROM crawl_day"))).scalar() or 0


async def pending_before(session: AsyncSession, day: date) -> int:
    """Ngày có phút mở trước `day` chưa nạp được (pending/error) — vùng chỉ Dukascopy có."""
    return (
        await session.execute(
            text("""
                SELECT count(*) FROM crawl_day
                WHERE day < :d AND calendar_minutes > 0 AND status IN ('pending', 'error')
            """),
            {"d": day},
        )
    ).scalar() or 0
