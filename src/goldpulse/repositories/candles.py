"""candle_1m + session_bar — upsert hàng loạt và tổng hợp phiên."""

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations.dukascopy import RawCandle

# Nến Dukascopy luôn đè (kể cả nến tạm goldapi). Một câu lệnh với mảng unnest — không vòng lặp từng dòng.
_UPSERT_FINAL = text("""
    INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
    SELECT t.ts, t.o, t.h, t.l, t.c, t.v, 'dukascopy', true, now()
    FROM unnest(
        CAST(:ts AS timestamptz[]), CAST(:o AS float8[]), CAST(:h AS float8[]),
        CAST(:l AS float8[]), CAST(:c AS float8[]), CAST(:v AS float8[])
    ) AS t(ts, o, h, l, c, v)
    ON CONFLICT (ts) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
        volume = EXCLUDED.volume, source = 'dukascopy', is_final = true, updated_at = now()
""")

# Nến tạm goldapi KHÔNG BAO GIỜ đè nến final (ARCHITECTURE §6).
_UPSERT_TEMP = text("""
    INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
    VALUES (:ts, :o, :h, :l, :c, 0, 'goldapi', false, now())
    ON CONFLICT (ts) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
        updated_at = now()
    WHERE candle_1m.is_final = false
""")

# Phiên chỉ được ghi khi có ít nhất một phút có giao dịch thật (volume > 0) hoặc nến realtime —
# khung phiên rơi vào cuối tuần / lễ chỉ có nến đứng giá thì không phải một phiên (DECISIONS 2026-09-24).
_UPSERT_SESSION = text("""
    INSERT INTO session_bar (session, trade_date, open_ts, close_ts, open, high, low, close, range,
                             direction, minutes_present, minutes_traded, is_final, updated_at)
    SELECT :session, :trade_date, :open_ts, :close_ts, a.o, a.h, a.l, a.c, a.h - a.l,
           sign(round((a.c - a.o)::numeric, 3))::smallint, a.n, a.nt, a.fin, now()
    FROM (
        SELECT (array_agg(open ORDER BY ts))[1] AS o, max(high) AS h, min(low) AS l,
               (array_agg(close ORDER BY ts DESC))[1] AS c, count(*) AS n,
               count(*) FILTER (WHERE volume > 0) AS nt, bool_and(is_final) AS fin,
               count(*) FILTER (WHERE volume > 0 OR source = 'goldapi') AS live
        FROM candle_1m WHERE ts >= :open_ts AND ts < :close_ts
    ) a
    WHERE a.live > 0
    ON CONFLICT (session, trade_date) DO UPDATE SET
        open_ts = EXCLUDED.open_ts, close_ts = EXCLUDED.close_ts, open = EXCLUDED.open,
        high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, range = EXCLUDED.range,
        direction = EXCLUDED.direction, minutes_present = EXCLUDED.minutes_present,
        minutes_traded = EXCLUDED.minutes_traded, is_final = EXCLUDED.is_final, updated_at = now()
""")


async def upsert_final(session: AsyncSession, candles: Sequence[RawCandle]) -> int:
    if not candles:
        return 0
    await session.execute(
        _UPSERT_FINAL,
        {
            "ts": [c.ts for c in candles],
            "o": [c.open for c in candles],
            "h": [c.high for c in candles],
            "l": [c.low for c in candles],
            "c": [c.close for c in candles],
            "v": [c.volume for c in candles],
        },
    )
    return len(candles)


async def upsert_temp(session: AsyncSession, ts: datetime, o: float, h: float, lo: float, c: float) -> None:
    await session.execute(_UPSERT_TEMP, {"ts": ts, "o": o, "h": h, "l": lo, "c": c})


async def recompute_session(
    session: AsyncSession, name: str, trade_date: date, open_ts: datetime, close_ts: datetime
) -> None:
    """Tính lại một session_bar theo khoá — idempotent. Không đủ điều kiện thì xoá dòng cũ."""
    params = {"session": name, "trade_date": trade_date, "open_ts": open_ts, "close_ts": close_ts}
    res = await session.execute(_UPSERT_SESSION, params)
    if res.rowcount == 0:  # type: ignore[attr-defined]
        await session.execute(
            text("DELETE FROM session_bar WHERE session = :session AND trade_date = :trade_date"),
            params,
        )


async def latest_candle_ts(session: AsyncSession) -> datetime | None:
    return (await session.execute(text("SELECT max(ts) FROM candle_1m"))).scalar()


async def session_stats(session: AsyncSession, name: str, year_from: int, year_to: int, isodow: int | None) -> dict:
    """Thống kê phiên trên session_bar (đã tính sẵn) — câu tra của S3."""
    sql = """
        SELECT count(*) AS n,
               avg(range) AS avg_range,
               count(*) FILTER (WHERE direction > 0) AS up,
               count(*) FILTER (WHERE direction < 0) AS down,
               count(*) FILTER (WHERE direction = 0) AS flat,
               min(trade_date) AS first_day, max(trade_date) AS last_day,
               array_agg(DISTINCT extract(year FROM trade_date)::int) AS years
        FROM session_bar
        WHERE session = :session
          AND close_ts <= now()  -- phiên đang chạy dở chưa có biên độ / hướng thật
          AND trade_date >= make_date(:y1, 1, 1) AND trade_date < make_date(:y2 + 1, 1, 1)
    """
    params: dict = {"session": name, "y1": year_from, "y2": year_to}
    if isodow is not None:
        sql += " AND extract(isodow FROM trade_date) = :dow"
        params["dow"] = isodow
    row = (await session.execute(text(sql), params)).mappings().one()
    return dict(row)


async def data_years(session: AsyncSession) -> tuple[int | None, int | None]:
    row = (
        await session.execute(
            text("""
                SELECT extract(year FROM min(trade_date))::int, extract(year FROM max(trade_date))::int
                FROM session_bar
            """)
        )
    ).one()
    return row[0], row[1]
