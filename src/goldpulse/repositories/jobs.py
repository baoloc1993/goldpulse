"""bot_job — hàng chờ trong Postgres giữa web/worker và tiến trình não (ARCHITECTURE §6b)."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

STUCK_S = 420  # running quá lâu (não chết giữa chừng) → về pending; > 120s × 3 lượt SDK thử lại (review 2026-09-26)
MAX_ATTEMPTS = 3


async def enqueue(session: AsyncSession, kind: str, payload: dict[str, Any]) -> int | None:
    """Thêm job. None = trùng (dự đoán tự động cùng bar_ts — worker khởi động lại giữa nến)."""
    try:
        async with session.begin_nested():
            row = await session.execute(
                text("""
                    INSERT INTO bot_job (kind, payload, status, attempts, created_at)
                    VALUES (:k, CAST(:p AS jsonb), 'pending', 0, now()) RETURNING id
                """),
                {"k": kind, "p": json.dumps(payload, default=str)},
            )
            return int(row.scalar_one())
    except IntegrityError:
        return None


async def claim(session: AsyncSession) -> dict[str, Any] | None:
    """Lấy một job pending — chat trước, rồi cũ nhất; SKIP LOCKED để hai tiến trình não không xử trùng."""
    row = (
        (
            await session.execute(
                text("""
                    UPDATE bot_job SET status = 'running', started_at = now(), attempts = attempts + 1
                    WHERE id = (
                        SELECT id FROM bot_job WHERE status = 'pending'
                        ORDER BY (kind = 'chat') DESC, created_at
                        FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    RETURNING id, kind, payload, attempts, created_at
                """)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def requeue_stuck(session: AsyncSession) -> int:
    """running > STUCK_S → pending (còn lượt) hoặc error (hết 3 lượt)."""
    res = await session.execute(
        text("""
            UPDATE bot_job SET
                status = CASE WHEN attempts >= :max THEN 'error' ELSE 'pending' END,
                last_error = CASE WHEN attempts >= :max THEN 'Não bot dừng giữa chừng 3 lần' ELSE last_error END,
                finished_at = CASE WHEN attempts >= :max THEN now() ELSE finished_at END
            WHERE status = 'running' AND started_at < now() - make_interval(secs => :s)
        """),
        {"s": STUCK_S, "max": MAX_ATTEMPTS},
    )
    return res.rowcount  # type: ignore[attr-defined]


async def expire_pending(session: AsyncSession, kind: str, older_than_s: float, reason: str) -> list[int]:
    """Job chờ quá lâu → error. Trả id để phía chat cập nhật tin nhắn tương ứng."""
    rows = await session.execute(
        text("""
            UPDATE bot_job SET status = 'error', last_error = :r, finished_at = now()
            WHERE status = 'pending' AND kind = :k AND created_at < now() - make_interval(secs => :s)
            RETURNING id
        """),
        {"k": kind, "s": older_than_s, "r": reason},
    )
    return [r[0] for r in rows]


async def finish(session: AsyncSession, job_id: int, result: dict[str, Any]) -> None:
    await session.execute(
        text("""
            UPDATE bot_job SET status = 'done', result = CAST(:r AS jsonb), finished_at = now(), last_error = NULL
            WHERE id = :id
        """),
        {"id": job_id, "r": json.dumps(result, default=str)},
    )


async def fail(session: AsyncSession, job_id: int, error: str, retry: bool) -> str:
    """Lỗi một lượt: còn lượt và `retry` → pending (não thử lại sau), không thì error. Trả trạng thái mới."""
    row = await session.execute(
        text("""
            UPDATE bot_job SET
                status = CASE WHEN :retry AND attempts < :max THEN 'pending' ELSE 'error' END,
                last_error = :e,
                finished_at = CASE WHEN :retry AND attempts < :max THEN NULL ELSE now() END
            WHERE id = :id RETURNING status
        """),
        {"id": job_id, "e": error[:2000], "retry": retry, "max": MAX_ATTEMPTS},
    )
    return str(row.scalar_one())


async def counts(session: AsyncSession, since: datetime) -> dict[str, int]:
    rows = await session.execute(
        text("""
            SELECT kind || ':' || status, count(*) FROM bot_job WHERE created_at >= :since GROUP BY 1
        """),
        {"since": since},
    )
    return {k: int(v) for k, v in rows}


async def brain_failures(session: AsyncSession, kind: str, skip_prefix: str) -> int:
    """Job lỗi vì não (không tính job bỏ có chủ đích — last_error bắt đầu bằng `skip_prefix`)."""
    return int(
        (
            await session.execute(
                text("""
                    SELECT count(*) FROM bot_job
                    WHERE kind = :k AND status = 'error' AND coalesce(last_error, '') NOT LIKE :p
                """),
                {"k": kind, "p": skip_prefix + "%"},
            )
        ).scalar()
        or 0
    )


async def pending_count(session: AsyncSession) -> int:
    return int(
        (await session.execute(text("SELECT count(*) FROM bot_job WHERE status IN ('pending', 'running')"))).scalar()
        or 0
    )
