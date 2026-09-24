"""feed_sample · query_log · worker_status — số đo và trạng thái vận hành."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.calendar import MARKET_OPEN_SQL


async def insert_sample(
    session: AsyncSession,
    polled_at: datetime,
    source_ts: datetime | None,
    price: float | None,
    lag: float | None,
    ok: bool,
) -> None:
    await session.execute(
        text("""
            INSERT INTO feed_sample (polled_at, source_ts, price, lag_seconds, ok)
            VALUES (:p, :s, :price, :lag, :ok) ON CONFLICT (polled_at) DO NOTHING
        """),
        {"p": polled_at, "s": source_ts, "price": price, "lag": lag, "ok": ok},
    )


async def latest_sample(session: AsyncSession) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("""
                SELECT polled_at, source_ts, price, lag_seconds, ok FROM feed_sample
                ORDER BY polled_at DESC LIMIT 1
            """)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def latest_price_sample(session: AsyncSession) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("""
                SELECT polled_at, source_ts, price FROM feed_sample
                WHERE price IS NOT NULL ORDER BY polled_at DESC LIMIT 1
            """)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def ok_prices_in(session: AsyncSession, start: datetime, end: datetime) -> list[float]:
    """Giá của các mẫu tươi (ok) trong [start, end), theo thứ tự thời gian — nguyên liệu nến tạm."""
    res = await session.execute(
        text("""
            SELECT price FROM feed_sample
            WHERE polled_at >= :a AND polled_at < :b AND ok AND price IS NOT NULL
            ORDER BY polled_at
        """),
        {"a": start, "b": end},
    )
    return [r[0] for r in res]


async def latency_metric(session: AsyncSession, since: datetime) -> tuple[int, int]:
    """(phút thị trường mở từ `since` tới phút vừa xong, số phút trong đó mọi mẫu ok).

    Phút không có mẫu nào (worker chết) tính là trượt. Ngày `empty` (lễ) bị loại.
    """
    row = (
        await session.execute(
            text(f"""
                WITH m AS (
                    SELECT generate_series(date_trunc('minute', CAST(:since AS timestamptz)) + interval '1 minute',
                                           date_trunc('minute', now()) - interval '1 minute',
                                           interval '1 minute') AS t
                ),
                open_m AS (
                    SELECT t FROM m WHERE {MARKET_OPEN_SQL}
                      AND (t AT TIME ZONE 'UTC')::date NOT IN (SELECT day FROM crawl_day WHERE status = 'empty')
                ),
                s AS (
                    SELECT date_trunc('minute', polled_at) AS t, bool_and(ok) AS good
                    FROM feed_sample WHERE polled_at >= :since GROUP BY 1
                )
                SELECT count(*), count(*) FILTER (WHERE s.good) FROM open_m LEFT JOIN s USING (t)
            """),
            {"since": since},
        )
    ).one()
    return int(row[0]), int(row[1])


async def insert_query_log(session: AsyncSession, params: dict[str, Any], duration_ms: float, rows: int) -> None:
    await session.execute(
        text("""
            INSERT INTO query_log (created_at, params, duration_ms, result_rows)
            VALUES (now(), CAST(:p AS jsonb), :d, :r)
        """),
        {"p": json.dumps(params), "d": duration_ms, "r": rows},
    )


async def query_speed_p95(session: AsyncSession, min_span_years: int) -> tuple[float | None, int]:
    row = (
        await session.execute(
            text("""
                SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), count(*)
                FROM query_log WHERE (params->>'span_years')::int >= :span
            """),
            {"span": min_span_years},
        )
    ).one()
    return (float(row[0]) if row[0] is not None else None), int(row[1])


async def cleanup(session: AsyncSession) -> None:
    """Dọn feed_sample / query_log cũ hơn 30 ngày (ARCHITECTURE §6)."""
    await session.execute(text("DELETE FROM feed_sample WHERE polled_at < now() - interval '30 days'"))
    await session.execute(text("DELETE FROM query_log WHERE created_at < now() - interval '30 days'"))


async def get_status(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = (
        await session.execute(text("SELECT value, updated_at FROM worker_status WHERE key = :k"), {"k": key})
    ).first()
    if not row:
        return None
    return {**row[0], "updated_at": row[1]}


async def set_status(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    await session.execute(
        text("""
            INSERT INTO worker_status (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """),
        {"k": key, "v": json.dumps(value, default=str)},
    )


async def set_status_if_absent(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    await session.execute(
        text("""
            INSERT INTO worker_status (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), now())
            ON CONFLICT (key) DO NOTHING
        """),
        {"k": key, "v": json.dumps(value, default=str)},
    )


async def db_ping(session: AsyncSession) -> None:
    await session.execute(text("SELECT 1"))
