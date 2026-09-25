"""S5 Chat phía web (ARCHITECTURE §7 E1–E3, E7): ghi câu hỏi + xếp job cho não; web KHÔNG gọi Claude."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import chat as chat_repo
from ..repositories import fed as fed_repo
from ..repositories import feed as feed_repo
from ..repositories import jobs as job_repo
from ..repositories import predictions as pred_repo
from . import fed
from .calendar import SESSIONS, VN, active_sessions
from .context_pack import live_state

BRAIN_STALE_S = 120  # nhịp tim não 30s — cũ hơn 2 phút là não chưa chạy (PRD §7)
BRAIN_ERROR_WINDOW_S = 600  # lỗi trong 10 phút gần nhất → huy hiệu đỏ "lỗi lần cuối"
MAX_LEN = 2000
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
THREAD_LIMIT = 200


class ChatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def brain_view(st: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if not st:
        return {"state": "down", "backend": None, "model": None, "last_error": None}
    age = (now - st["updated_at"]).total_seconds()
    err_at = datetime.fromisoformat(st["last_error_at"]) if st.get("last_error_at") else None
    ok_at = datetime.fromisoformat(st["last_ok_at"]) if st.get("last_ok_at") else None
    # Đỏ chỉ khi lỗi gần đây VÀ chưa có câu trả lời thành công nào sau lỗi đó
    recent_err = (
        err_at is not None
        and (now - err_at).total_seconds() < BRAIN_ERROR_WINDOW_S
        and (ok_at is None or ok_at < err_at)
    )
    state = "down" if age > BRAIN_STALE_S else "error" if recent_err else "ok"
    return {
        "state": state,
        "backend": st.get("backend"),
        "model": st.get("model"),
        "last_error": st.get("last_error"),
        "last_error_at": st.get("last_error_at"),
        "seen_seconds_ago": round(age),
    }


async def worker_alive(session: AsyncSession, now: datetime) -> bool:
    """Worker ghi nhịp tim 30s (worker_status.worker) — cũ > 2 phút là máy nạp dữ liệu đang dừng."""
    w = await feed_repo.get_status(session, "worker")
    return bool(w and (now - w["updated_at"]).total_seconds() < BRAIN_STALE_S)


async def header(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    """Đầu màn S5: giá + độ trễ · phiên đang chạy · FED gần nhất · não bot."""
    now = now or datetime.now(UTC)
    live = await live_state(session, now)
    sample = await feed_repo.latest_price_sample(session)
    last_fed = await fed_repo.with_reactions(session, limit=1, before=now)
    brain = await feed_repo.get_status(session, "brain")
    return {
        "price": {
            **live,
            "source_ts": sample["source_ts"].isoformat() if sample and sample["source_ts"] else None,
        },
        "sessions": [
            {"key": k, "label": SESSIONS[k].label, "closes_vn": c.astimezone(VN).strftime("%H:%M")}
            for k, _, c in active_sessions(now)
        ],
        "fed": fed.reaction_row(last_fed[0]) if last_fed else None,
        "next_meeting": fed.next_meeting(now),
        "brain": brain_view(brain, now),
        "worker_alive": await worker_alive(session, now),
        "pending_jobs": await job_repo.pending_count(session),
    }


async def message_view(session: AsyncSession, m: dict[str, Any]) -> dict[str, Any]:
    pred = None
    if m["prediction_id"]:
        p = await pred_repo.get(session, m["prediction_id"])
        if p:
            pred = {
                "id": p["id"],
                "direction": p["direction"],
                "due_vn": p["due_at"].astimezone(VN).strftime("%H:%M"),
                "outcome": p["outcome"],
            }
    return {
        "id": m["id"],
        "thread_id": m["thread_id"],
        "role": m["role"],
        "content": m["content"],
        "status": m["status"],
        "prediction": pred,
        "created_vn": m["created_at"].astimezone(VN).strftime("%d/%m %H:%M"),
        "attempts": m.get("attempts") or 0,
        "waited_seconds": round((datetime.now(UTC) - m["created_at"]).total_seconds()),
    }


async def thread_view(session: AsyncSession, thread_id: int | None) -> dict[str, Any]:
    thread = await chat_repo.get_thread(session, thread_id) if thread_id else await chat_repo.latest_thread(session)
    if thread_id and not thread:
        raise ChatError(404, "Không có đoạn chat này.")
    if not thread:
        return {"thread_id": None, "messages": []}
    msgs = await chat_repo.messages(session, thread["id"], THREAD_LIMIT)
    return {"thread_id": thread["id"], "messages": [await message_view(session, m) for m in msgs]}


async def post(session: AsyncSession, thread_id: int | None, content: str) -> dict[str, Any]:
    """Một lần bấm Gửi = một câu hỏi + một câu trả lời pending + một job (không dedupe — chat là hội thoại)."""
    # str.strip() không bỏ ký tự rộng 0 (U+200B…U+200D, U+2060, U+FEFF) — câu chỉ có chúng là câu rỗng
    text_ = content.translate(ZERO_WIDTH).strip()
    if not text_:
        raise ChatError(422, "Gõ câu hỏi trước khi gửi.")
    if len(text_) > MAX_LEN:
        raise ChatError(422, f"Câu hỏi dài quá — tối đa {MAX_LEN} ký tự.")
    if thread_id is not None:
        if not await chat_repo.get_thread(session, thread_id):
            raise ChatError(404, "Không có đoạn chat này — tải lại trang.")
    else:
        latest = await chat_repo.latest_thread(session)
        thread_id = latest["id"] if latest else await chat_repo.create_thread(session, "")
    await chat_repo.touch_thread(session, thread_id, text_[:60])
    q_id = await chat_repo.add_message(session, thread_id, "user", text_, "ok")
    a_id = await chat_repo.add_message(session, thread_id, "assistant", "", "pending")
    job = await job_repo.enqueue(session, "chat", {"thread_id": thread_id, "message_id": a_id})
    assert job is not None  # chỉ job dự đoán tự động có ràng buộc trùng
    await chat_repo.set_job(session, a_id, job)
    q = await chat_repo.get_message(session, q_id)
    a = await chat_repo.get_message(session, a_id)
    return {
        "thread_id": thread_id,
        "question": await message_view(session, q),  # type: ignore[arg-type]
        "answer": await message_view(session, a),  # type: ignore[arg-type]
    }


async def get(session: AsyncSession, message_id: int) -> dict[str, Any]:
    m = await chat_repo.get_message(session, message_id)
    if not m:
        raise ChatError(404, "Không có tin nhắn này.")
    return await message_view(session, m)


async def resend(session: AsyncSession, message_id: int) -> dict[str, Any]:
    """'Gửi lại' trên câu trả lời lỗi: chính câu trả lời đó về pending + job mới; câu hỏi giữ nguyên."""
    m = await chat_repo.get_message(session, message_id)
    if not m or m["role"] != "assistant":
        raise ChatError(404, "Không có câu trả lời này.")
    if m["status"] != "error" or not await chat_repo.reopen_if_error(session, m["id"]):
        raise ChatError(409, "Câu trả lời này không ở trạng thái lỗi.")
    job = await job_repo.enqueue(session, "chat", {"thread_id": m["thread_id"], "message_id": m["id"]})
    assert job is not None
    await chat_repo.set_job(session, m["id"], job)
    await chat_repo.touch_thread(session, m["thread_id"], "")
    return await get(session, m["id"])


async def new_thread(session: AsyncSession) -> dict[str, Any]:
    """'Chat mới' chỉ mở thread mới, không xoá gì. Thread rỗng hiện tại thì dùng lại (bấm hai lần không đẻ rác)."""
    latest = await chat_repo.latest_thread(session)
    if latest and not await chat_repo.messages(session, latest["id"], 1):
        return {"thread_id": latest["id"]}
    return {"thread_id": await chat_repo.create_thread(session, "")}
