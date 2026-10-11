"""fed_decision — upsert kỳ họp + phản ứng vàng quanh giờ công bố."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_many(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    """Idempotent theo meeting_date — nạp lại hằng ngày không đẻ dòng trùng."""
    if not rows:
        return
    await session.execute(
        text("""
            INSERT INTO fed_decision (meeting_date, announced_at, rate_lower, rate_upper, change_bp, kind,
                                      source_note, loaded_at)
            VALUES (:meeting_date, :announced_at, :rate_lower, :rate_upper, :change_bp, :kind, :source_note, now())
            ON CONFLICT (meeting_date) DO UPDATE SET
                announced_at = EXCLUDED.announced_at, rate_lower = EXCLUDED.rate_lower,
                rate_upper = EXCLUDED.rate_upper, change_bp = EXCLUDED.change_bp, kind = EXCLUDED.kind,
                source_note = EXCLUDED.source_note, loaded_at = now()
        """),
        list(rows),
    )


async def count(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM fed_decision"))).scalar() or 0)


# Giá tại mốc = close nến phút SỐNG cuối trước mốc, trong 5 phút (thiếu → null, kỳ đó bị loại khỏi trung bình).
_LIVE = "(volume > 0 OR source = 'goldapi')"
_REACTIONS = f"""
    SELECT f.meeting_date, f.announced_at, f.rate_lower, f.rate_upper, f.change_bp, f.kind,
           p0.close AS p0, p15.close AS p15, p60.close AS p60, p1d.close AS p1d
    FROM fed_decision f
    LEFT JOIN LATERAL (SELECT close FROM candle_1m WHERE {_LIVE} AND ts < f.announced_at
                       AND ts >= f.announced_at - interval '5 minutes' ORDER BY ts DESC LIMIT 1) p0 ON true
    LEFT JOIN LATERAL (SELECT close FROM candle_1m WHERE {_LIVE} AND ts < f.announced_at + interval '15 minutes'
                       AND ts >= f.announced_at + interval '10 minutes' ORDER BY ts DESC LIMIT 1) p15 ON true
    LEFT JOIN LATERAL (SELECT close FROM candle_1m WHERE {_LIVE} AND ts < f.announced_at + interval '1 hour'
                       AND ts >= f.announced_at + interval '55 minutes' ORDER BY ts DESC LIMIT 1) p60 ON true
    LEFT JOIN LATERAL (SELECT close FROM candle_1m WHERE {_LIVE} AND ts < f.announced_at + interval '1 day'
                       AND ts >= f.announced_at + interval '1 day' - interval '5 minutes'
                       ORDER BY ts DESC LIMIT 1) p1d ON true
"""


async def with_reactions(
    session: AsyncSession, limit: int | None = None, change: str | None = None, before: datetime | None = None
) -> list[dict[str, Any]]:
    """Kỳ họp mới nhất trước, kèm giá tại công bố / +15m / +1h / +1 ngày. change: 'cut' | 'hike' | None."""
    where = []
    params: dict[str, Any] = {}
    if change == "cut":
        where.append("f.change_bp < 0")
    elif change == "hike":
        where.append("f.change_bp > 0")
    if before is not None:
        where.append("f.announced_at <= :before")
        params["before"] = before
    sql = _REACTIONS + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY f.meeting_date DESC"
    if limit:
        sql += " LIMIT :lim"
        params["lim"] = limit
    rows = (await session.execute(text(sql), params)).mappings()
    return [dict(r) for r in rows]
