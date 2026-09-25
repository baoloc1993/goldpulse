"""Lỗi hệ thống "tự báo về một nơi" khi chưa bật Sentry: ghi lỗi gần nhất vào worker_status để S1 hiện ra
(DECISIONS 2026-09-25). Chi tiết đầy đủ vẫn nằm trong log JSON (`docker compose logs`)."""

import structlog

from ..db import session_scope
from ..repositories import feed as feed_repo

log = structlog.get_logger("errors")


async def record_error(where: str, exc: BaseException) -> None:
    try:
        async with session_scope() as s:
            await feed_repo.set_status(s, "last_error", {"where": where, "error": f"{type(exc).__name__}: {exc}"[:300]})
    except Exception:
        log.exception("record_error_failed", where=where)  # DB chết thì chỉ còn log — không ném tiếp
