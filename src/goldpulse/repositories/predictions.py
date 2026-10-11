"""prediction — sổ dự đoán + chấm. Bất biến sau khi chấm; không có đường xoá."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def insert(session: AsyncSession, row: dict[str, Any]) -> int:
    res = await session.execute(
        text("""
            INSERT INTO prediction (made_at, source, direction, confidence, price_at, horizon_minutes, due_at,
                                    outcome, signals, reasoning, backend, model, job_id)
            VALUES (:made_at, :source, :direction, :confidence, :price_at, :horizon_minutes, :due_at,
                    'pending', CAST(:signals AS jsonb), :reasoning, :backend, :model, :job_id)
            RETURNING id
        """),
        {**row, "signals": json.dumps(row.get("signals") or {}, default=str)},
    )
    return int(res.scalar_one())


async def due(session: AsyncSession, grace_s: float) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("""
                SELECT id, direction, price_at, due_at FROM prediction
                WHERE outcome = 'pending' AND due_at + make_interval(secs => :g) <= now()
                ORDER BY due_at LIMIT 500
            """),
            {"g": grace_s},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def score(session: AsyncSession, pid: int, price_due: float | None, outcome: str) -> None:
    """Chỉ chấm dự đoán còn pending — chấm xong không chấm lại."""
    await session.execute(
        text("""
            UPDATE prediction SET price_due = :p, outcome = :o, scored_at = now()
            WHERE id = :id AND outcome = 'pending'
        """),
        {"id": pid, "p": price_due, "o": outcome},
    )


async def stats(session: AsyncSession) -> dict[str, Any]:
    # Dự đoán của backend `fake` (test) không bao giờ tính vào metric GO/KILL
    rows = await session.execute(
        text(
            "SELECT source, direction, outcome, count(*) FROM prediction"
            " WHERE backend IS DISTINCT FROM 'fake' GROUP BY 1, 2, 3"
        )
    )
    return {"rows": [{"source": s, "direction": d, "outcome": o, "n": int(n)} for s, d, o, n in rows]}


async def page(session: AsyncSession, before_id: int | None, limit: int) -> list[dict[str, Any]]:
    sql = "SELECT * FROM prediction"
    params: dict[str, Any] = {"n": limit}
    if before_id is not None:
        sql += " WHERE id < :b"
        params["b"] = before_id
    sql += " ORDER BY id DESC LIMIT :n"
    rows = (await session.execute(text(sql), params)).mappings()
    return [dict(r) for r in rows]


async def get(session: AsyncSession, pid: int) -> dict[str, Any] | None:
    row = (await session.execute(text("SELECT * FROM prediction WHERE id = :id"), {"id": pid})).mappings().first()
    return dict(row) if row else None


async def last_made_at(session: AsyncSession) -> datetime | None:
    return (await session.execute(text("SELECT max(made_at) FROM prediction"))).scalar()
