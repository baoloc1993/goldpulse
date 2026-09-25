"""chat_thread + chat_message (ARCHITECTURE §6b). Không có đường xoá."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_MSG_COLS = (
    "id, thread_id, role, content, status, job_id, prediction_id, usage, created_at,"
    " (SELECT attempts FROM bot_job WHERE bot_job.id = chat_message.job_id) AS attempts"
)


async def latest_thread(session: AsyncSession) -> dict[str, Any] | None:
    row = (
        (await session.execute(text("SELECT * FROM chat_thread ORDER BY last_message_at DESC, id DESC LIMIT 1")))
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def get_thread(session: AsyncSession, thread_id: int) -> dict[str, Any] | None:
    row = (
        (await session.execute(text("SELECT * FROM chat_thread WHERE id = :id"), {"id": thread_id})).mappings().first()
    )
    return dict(row) if row else None


async def create_thread(session: AsyncSession, title: str) -> int:
    row = await session.execute(
        text("""
            INSERT INTO chat_thread (created_at, last_message_at, title) VALUES (now(), now(), :t) RETURNING id
        """),
        {"t": title},
    )
    return int(row.scalar_one())


async def touch_thread(session: AsyncSession, thread_id: int, title_if_empty: str) -> None:
    await session.execute(
        text("""
            UPDATE chat_thread SET last_message_at = now(),
                   title = CASE WHEN title = '' THEN :t ELSE title END
            WHERE id = :id
        """),
        {"id": thread_id, "t": title_if_empty},
    )


async def add_message(
    session: AsyncSession, thread_id: int, role: str, content: str, status: str, job_id: int | None = None
) -> int:
    row = await session.execute(
        text("""
            INSERT INTO chat_message (thread_id, role, content, status, job_id, created_at)
            VALUES (:t, :r, :c, :s, :j, now()) RETURNING id
        """),
        {"t": thread_id, "r": role, "c": content, "s": status, "j": job_id},
    )
    return int(row.scalar_one())


async def set_job(session: AsyncSession, message_id: int, job_id: int) -> None:
    await session.execute(text("UPDATE chat_message SET job_id = :j WHERE id = :id"), {"id": message_id, "j": job_id})


async def get_message(session: AsyncSession, message_id: int) -> dict[str, Any] | None:
    row = (
        (await session.execute(text(f"SELECT {_MSG_COLS} FROM chat_message WHERE id = :id"), {"id": message_id}))
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def messages(session: AsyncSession, thread_id: int, limit: int) -> list[dict[str, Any]]:
    """`limit` tin nhắn gần nhất của thread, cũ → mới."""
    rows = (
        await session.execute(
            text(f"""
                SELECT * FROM (SELECT {_MSG_COLS} FROM chat_message WHERE thread_id = :t ORDER BY id DESC LIMIT :n) x
                ORDER BY id
            """),
            {"t": thread_id, "n": limit},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def messages_before(session: AsyncSession, thread_id: int, before_id: int, limit: int) -> list[dict[str, Any]]:
    """`limit` tin nhắn `ok` gần nhất TRƯỚC `before_id` (cũ → mới) — lịch sử cho não, kể cả khi "Gửi lại" câu cũ."""
    rows = (
        await session.execute(
            text(f"""
                SELECT * FROM (
                    SELECT {_MSG_COLS} FROM chat_message
                    WHERE thread_id = :t AND id < :b AND status = 'ok'
                    ORDER BY id DESC LIMIT :n
                ) x ORDER BY id
            """),
            {"t": thread_id, "b": before_id, "n": limit},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def question_for(session: AsyncSession, answer_id: int) -> dict[str, Any] | None:
    """Câu hỏi của người dùng ngay trước câu trả lời `answer_id` trong cùng thread."""
    row = (
        (
            await session.execute(
                text(f"""
                    SELECT {_MSG_COLS} FROM chat_message
                    WHERE role = 'user' AND id < :id
                      AND thread_id = (SELECT thread_id FROM chat_message WHERE id = :id)
                    ORDER BY id DESC LIMIT 1
                """),
                {"id": answer_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def complete(
    session: AsyncSession,
    message_id: int,
    content: str,
    status: str,
    context_pack: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    prediction_id: int | None,
) -> None:
    await session.execute(
        text("""
            UPDATE chat_message SET content = :c, status = :s, context_pack = CAST(:cp AS jsonb),
                   usage = CAST(:u AS jsonb), prediction_id = :p
            WHERE id = :id
        """),
        {
            "id": message_id,
            "c": content,
            "s": status,
            "cp": json.dumps(context_pack, default=str) if context_pack is not None else None,
            "u": json.dumps(usage, default=str) if usage is not None else None,
            "p": prediction_id,
        },
    )


async def fail_by_jobs(session: AsyncSession, job_ids: list[int], content: str) -> None:
    if not job_ids:
        return
    await session.execute(
        text("""
            UPDATE chat_message SET status = 'error', content = :c
            WHERE role = 'assistant' AND status = 'pending' AND job_id = ANY(:ids)
        """),
        {"ids": job_ids, "c": content},
    )


async def fail_orphans(session: AsyncSession, content: str) -> int:
    """Câu trả lời còn `pending` mà job đã `error` (requeue hết lượt, não chết) → lỗi, để hiện nút "Gửi lại"."""
    res = await session.execute(
        text("""
            UPDATE chat_message m SET status = 'error', content = :c
            FROM bot_job j
            WHERE m.job_id = j.id AND m.role = 'assistant' AND m.status = 'pending' AND j.status = 'error'
        """),
        {"c": content},
    )
    return res.rowcount  # type: ignore[attr-defined]


async def count_today(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM chat_message WHERE role = 'user' AND created_at >= now() - interval '1 day'")
            )
        ).scalar()
        or 0
    )
