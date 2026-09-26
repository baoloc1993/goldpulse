"""Não bot: `python -m goldpulse.brain` (`make brain`) — chạy trên máy chủ, NGOÀI Docker khi backend `cli`.

Vòng lặp: lấy bot_job (SKIP LOCKED) → soạn gói dữ liệu → hỏi Claude (cli | api | fake) → ghi kết quả.
Nhịp tim 30s vào worker_status.brain — S5 báo "Não bot chưa chạy" khi nhịp tim cũ > 2 phút.
Web không bao giờ gọi Claude: não chết thì web + worker vẫn sống (AC-11).

`--production` (dùng bởi `make brain`): ép APP_ENV=production trước khi đọc cấu hình — backend `fake` bị chặn dù
`deployment/local/.env` (dùng chung với `make dev`) đang để APP_ENV=development.
"""

import os
import sys

if "--production" in sys.argv:
    os.environ["APP_ENV"] = "production"

import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import signal  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import structlog  # noqa: E402
from sqlalchemy import text  # noqa: E402

from ..config import settings  # noqa: E402
from ..db import engine, session_scope  # noqa: E402
from ..integrations.claude_base import Brain, BrainError, JobSkipped  # noqa: E402
from ..logs import setup_logging  # noqa: E402
from ..repositories import chat as chat_repo  # noqa: E402
from ..repositories import feed as feed_repo  # noqa: E402
from ..repositories import jobs as job_repo  # noqa: E402
from ..services import context_pack, predictions, signals  # noqa: E402
from ..services.errors import record_error  # noqa: E402

log = structlog.get_logger("brain")

HERE = Path(__file__).parent
DISCLAIMER = (
    "Đây là phân tích dữ liệu lịch sử, không phải lời khuyên đầu tư. "
    "GoldPulse không đặt lệnh trên bất kỳ tài khoản nào."
)
REFUSALS = {
    "not_gold": "Hiện mình chỉ có dữ liệu vàng (XAUUSD) — mình chưa trả lời được về tài sản khác.",
    "trade_request": "GoldPulse không đặt lệnh trên bất kỳ tài khoản nào — mình chỉ phân tích dữ liệu vàng.",
}
# Chữ có ý gợi ý mua/bán — khi không được gợi ý (đóng cửa/giá trễ) thì code thêm câu lưu ý
TRADE_WORDS = re.compile(
    r"\b(nên|có thể|hãy|cân nhắc)\s+(mua|bán|vào lệnh|chốt)|\bmua vào\b|\bbán ra\b|\bvào lệnh\b|\b(buy|sell)\b",
    re.IGNORECASE,
)
AUTO_MAX_AGE_S = 300  # job dự đoán tự động chờ quá 5 phút → giá đã trôi, bỏ nến đó
RETRY_PAUSE_S = 20
HEARTBEAT_S = 30


def load(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def make_backend() -> Brain:
    if settings.brain_backend == "fake":
        if settings.is_production:
            raise SystemExit("BRAIN_BACKEND=fake không được dùng ở production — đặt cli hoặc api.")
        from ..integrations.claude_fake import ClaudeFake

        return ClaudeFake()
    if settings.brain_backend == "api":
        if not settings.anthropic_api_key:
            raise SystemExit("BRAIN_BACKEND=api cần ANTHROPIC_API_KEY trong .env.")
        from ..integrations.claude_api import ClaudeApi

        return ClaudeApi(settings.anthropic_api_key, settings.claude_api_model, settings.brain_timeout_seconds)
    from ..integrations.claude_cli import ClaudeCli

    return ClaudeCli(
        settings.claude_cli_bin, settings.claude_cli_model, settings.brain_timeout_seconds, settings.claude_cli_effort
    )


def compose_answer(data: dict[str, Any], live: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Nội dung cuối + dự đoán được phép ghi. Luật do CODE giữ, không phụ thuộc não (AC-9):
    từ chối → câu chuẩn; không được gợi ý → bỏ dự đoán; có gợi ý → chèn dòng miễn trừ."""
    if not isinstance(data, dict):
        raise BrainError("Não trả dữ liệu không đúng dạng.")
    refusal = data.get("refusal_kind")
    if refusal in REFUSALS:
        return REFUSALS[refusal], None
    answer = str(data.get("answer") or "").strip()
    raw = data.get("prediction")
    pred = raw if isinstance(raw, dict) and raw.get("direction") in ("up", "down") else None
    if pred and pred.get("confidence") not in ("low", "medium", "high"):
        pred = {**pred, "confidence": "low"}
    # Câu có ý mua/bán (dự đoán, não tự khai, hoặc chữ) — chỉ để thêm câu lưu ý khi không được gợi ý
    trade_talk = bool(pred) or bool(data.get("suggests_trade")) or bool(TRADE_WORDS.search(answer))
    if not live.get("can_suggest"):
        if trade_talk:
            why = (
                "thị trường đang nghỉ" if live.get("no_suggest_reason") == "market_closed" else "giá realtime đang trễ"
            )
            answer = f"{answer}\n\nLưu ý: {why} — câu trả lời này không phải gợi ý mua/bán và không được ghi dự đoán."
        pred = None
    # Dòng miễn trừ cho MỌI câu trả lời không phải từ chối — không phụ thuộc cờ hay cách viết của não (AC-9)
    return f"{answer}\n\n---\n{DISCLAIMER}", pred


def user_error(e: BrainError) -> str:
    """Câu lỗi cho người đọc 10–30 giây: làm gì tiếp trước, chi tiết kỹ thuật sau (dogfood 2026-09-26)."""
    if e.retryable:
        head = "Bot chưa trả lời được sau 3 lần thử — bấm Gửi lại, hoặc thử lại sau vài phút."
    else:
        head = "Bot tạm không dùng được — cần xử lý trên máy chủ (xem `make doctor`), xong thì bấm Gửi lại."
    return f"{head}\nChi tiết: {e.message}"


class BrainLoop:
    def __init__(self, backend: Brain) -> None:
        self.backend = backend
        self.stop = asyncio.Event()
        self.chat_system = load("prompts/chat.md")
        self.predict_system = load("prompts/predict.md")
        self.chat_schema = json.loads(load("schemas/chat.json"))
        self.predict_schema = json.loads(load("schemas/predict.json"))
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.last_ok_at: str | None = None
        self.done = 0

    async def beat(self) -> None:
        async with session_scope() as s:
            await feed_repo.set_status(
                s,
                "brain",
                {
                    "backend": self.backend.name,
                    "model": self.backend.model,
                    "pid": os.getpid(),
                    "jobs_done": self.done,
                    "last_error": self.last_error,
                    "last_error_at": self.last_error_at,
                    "last_ok_at": self.last_ok_at,
                },
            )

    async def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.beat()
            except Exception:
                log.exception("heartbeat_failed")
            try:
                await asyncio.wait_for(self.stop.wait(), HEARTBEAT_S)
            except TimeoutError:
                pass

    def note_error(self, msg: str) -> None:
        self.last_error = msg
        self.last_error_at = datetime.now(UTC).isoformat()

    async def run(self) -> None:
        # Hai não trên một kho (vd make dev + make brain) → job requeue bị xử đôi: giữ advisory lock suốt đời não
        self.lock_conn = await engine.connect()
        if not (await self.lock_conn.execute(text("SELECT pg_try_advisory_lock(20260926)"))).scalar():
            await self.lock_conn.close()
            await engine.dispose()
            raise SystemExit("Đã có một não GoldPulse khác đang chạy trên kho này — dừng.")
        db = engine.url
        log.info(
            "brain_start", backend=self.backend.name, model=self.backend.model, db=f"{db.host}:{db.port}/{db.database}"
        )
        hb = asyncio.create_task(self.heartbeat_loop())
        while not self.stop.is_set():
            try:
                async with session_scope() as s:
                    job = await job_repo.claim(s)
                if not job:
                    try:
                        await asyncio.wait_for(self.stop.wait(), 2)
                    except TimeoutError:
                        pass
                    continue
                ok = await self.handle(job)
                if not ok:
                    await asyncio.sleep(RETRY_PAUSE_S)  # "thử lại tối đa 3 lần cách 20s" (ARCHITECTURE §6)
            except Exception as e:
                log.exception("brain_loop_error")
                await record_error("brain", e)
                await asyncio.sleep(10)
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)
        await self.backend.close()
        await self.lock_conn.close()
        await engine.dispose()
        log.info("brain_stop")

    async def handle(self, job: dict[str, Any]) -> bool:
        """True = xong (thành công hoặc bỏ hẳn); False = lỗi sẽ thử lại."""
        log.info("job_start", id=job["id"], kind=job["kind"], attempt=job["attempts"])
        try:
            if job["kind"] == "chat":
                await self.chat(job)
            else:
                await self.predict(job)
            self.done += 1
            self.last_ok_at = datetime.now(UTC).isoformat()
            return True
        except Exception as e:  # noqa: BLE001 — mọi lỗi của MỘT job đều phải kết thúc job, không để chat kẹt
            if not isinstance(e, BrainError):
                log.exception("job_crashed", id=job["id"])
                await record_error("brain/job", e)
                e = BrainError(f"Lỗi xử lý nội bộ ({type(e).__name__}).")
            if not isinstance(e, JobSkipped):
                self.note_error(e.message)
            log.warning("job_failed", id=job["id"], error=e.message, retryable=e.retryable)
            async with session_scope() as s:
                status = await job_repo.fail(s, job["id"], e.message, e.retryable)
                mid = job["payload"].get("message_id")
                if status == "error" and job["kind"] == "chat" and await chat_repo.current_job(s, mid) == job["id"]:
                    await chat_repo.complete(s, mid, user_error(e), "error", None, None, None)
            return status == "error"

    async def chat(self, job: dict[str, Any]) -> None:
        mid = job["payload"]["message_id"]
        now = datetime.now(UTC)
        async with session_scope() as s:
            q = await chat_repo.question_for(s, mid)
            pack = await context_pack.build_chat(s, job["payload"]["thread_id"], mid, now)
        user = json.dumps({"question": q["content"] if q else "", "data": pack}, ensure_ascii=False, default=str)
        reply = await self.backend.ask(self.chat_system, user, self.chat_schema)
        async with session_scope() as s:
            # Tin nhắn đã được "Gửi lại" sang job khác trong lúc chờ → job này hết phận sự, không ghi đè/không ghi
            # dự đoán thứ hai (khoá dòng tới hết transaction)
            if await chat_repo.current_job(s, mid) != job["id"]:
                await job_repo.finish(s, job["id"], {"superseded": True})
                return
            live = await context_pack.live_state(s, datetime.now(UTC))  # tình trạng LÚC ghi, không phải lúc hỏi
            answer, pred = compose_answer(reply.data, live)
            pid = None
            if pred:
                made = datetime.now(UTC)
                price = await predictions.price_at(s, made)
                if price is not None:
                    pid = await predictions.create(
                        s,
                        made_at=made,
                        source="chat",
                        direction=pred["direction"],
                        confidence=pred.get("confidence", "low"),
                        price=price,
                        signals=pack["signals_15m"],
                        reasoning=str(pred.get("reason") or ""),
                        backend=self.backend.name,
                        model=reply.usage.get("model"),
                        job_id=job["id"],
                    )
            await chat_repo.complete(s, mid, answer, "ok", pack, reply.usage, pid)
            await job_repo.finish(s, job["id"], {"prediction_id": pid, "usage": reply.usage})

    async def predict(self, job: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        bar_end = datetime.fromisoformat(job["payload"]["bar_ts"])
        if (now - job["created_at"]).total_seconds() > AUTO_MAX_AGE_S:
            raise JobSkipped("Quá hạn — job chờ não quá 5 phút, bỏ nến này.")
        async with session_scope() as s:
            live = await context_pack.live_state(s, now)
            if not live["can_suggest"] or not signals.auto_allowed(bar_end):
                raise JobSkipped("Thị trường sắp nghỉ hoặc giá realtime đang trễ — bỏ nến này.")
            pack = await context_pack.build_prediction(s, job["payload"], now)
        user = json.dumps({"data": pack}, ensure_ascii=False, default=str)
        reply = await self.backend.ask(self.predict_system, user, self.predict_schema)
        d = reply.data
        if d.get("direction") not in ("up", "down"):
            raise BrainError("Não trả hướng không hợp lệ.")
        async with session_scope() as s:
            made = datetime.now(UTC)
            price = await predictions.price_at(s, made)
            if price is None:
                raise JobSkipped("Không có nến phút gần nhất để lấy giá lúc dự đoán — bỏ nến này.")
            pid = await predictions.create(
                s,
                made_at=made,
                source="auto",
                direction=d["direction"],
                confidence=d.get("confidence", "low"),
                price=price,
                signals=job["payload"].get("signals") or {},
                reasoning=str(d.get("reason") or ""),
                backend=self.backend.name,
                model=reply.usage.get("model"),
                job_id=job["id"],
            )
            await job_repo.finish(s, job["id"], {"prediction_id": pid, "usage": reply.usage})
        log.info("prediction_made", id=pid, direction=d["direction"], due=(made + timedelta(minutes=15)).isoformat())


def main() -> None:
    setup_logging()
    loop_ = BrainLoop(make_backend())

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, loop_.stop.set)
            except NotImplementedError:  # Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(loop_.stop.set))
        await loop_.run()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
