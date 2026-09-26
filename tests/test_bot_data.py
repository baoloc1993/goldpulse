"""(v2) Luồng DỮ LIỆU của bot: tín hiệu → job · chấm dự đoán · tỉ lệ đúng · não xử job dự đoán · nạp FED · gói chat.

Chỗ sai ở đây là sổ dự đoán sai (tỉ lệ đúng — metric GO/KILL của PRD §6) hoặc bản ghi trùng.
Não chạy backend `fake` — không gọi mạng.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from goldpulse.integrations.fred import RatePoint
from tests.conftest import needs_db


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# Thứ Tư 23/09/2026 10:00 New York (mùa hè, UTC−4) — thị trường mở, xa giờ nghỉ 17:00
BAR_OPEN = utc(2026, 9, 23, 14, 0)
# 16:45 New York — 15 phút cuối trước giờ nghỉ hằng ngày
BAR_BEFORE_BREAK = utc(2026, 9, 23, 20, 45)


@pytest.fixture
async def db():
    from goldpulse.db import engine, session_scope

    async with session_scope() as s:
        await s.execute(
            text(
                "TRUNCATE chat_message, chat_thread, bot_job, prediction, fed_decision, candle_1m, feed_sample,"
                " worker_status RESTART IDENTITY CASCADE"
            )
        )
    yield session_scope
    await engine.dispose()


@pytest.fixture
async def worker(db):
    from goldpulse.worker.__main__ import Worker

    w = Worker()
    yield w
    await w.duka.close()
    await w.hd.close()
    await w.gold.close()


def freeze_worker_now(monkeypatch, now: datetime) -> None:
    """signal_once so mốc với datetime.now(UTC) — đóng băng đồng hồ của riêng module worker."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("goldpulse.worker.__main__.datetime", Frozen)


async def seed_falling_bars(scope, bar_end: datetime, n: int = 30) -> None:
    """Một nến phút có giao dịch trong mỗi khung 15m trước `bar_end`, close giảm dần → streak −n (tín hiệu bật)."""
    async with scope() as s:
        for i in range(n):
            ts = bar_end - timedelta(minutes=15 * (i + 1))
            await s.execute(
                text("""INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
                        VALUES (:t, :p, :p, :p, :p, 1, 'histdata', true, now())"""),
                {"t": ts, "p": 4300.0 + i},
            )


async def insert_candle(scope, ts: datetime, close: float) -> None:
    async with scope() as s:
        await s.execute(
            text("""INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
                    VALUES (:t, :c, :c, :c, :c, 1, 'histdata', true, now())"""),
            {"t": ts, "c": close},
        )


async def signal_jobs(scope) -> list[dict]:
    async with scope() as s:
        rows = await s.execute(text("SELECT id, payload FROM bot_job WHERE kind = 'signal_prediction' ORDER BY id"))
        return [dict(r._mapping) for r in rows]


async def signal_status(scope) -> dict | None:
    from goldpulse.repositories import feed as feed_repo

    async with scope() as s:
        return await feed_repo.get_status(s, "signal")


# --- 1. Worker: tín hiệu 15 phút → job dự đoán tự động ---------------------------------------------


async def _confident_model(session, bar_end):
    return {"model": "logit15m_v1", "p_up": 0.61, "direction": "up", "confidence": 0.11, "threshold": 0.08,
            "trigger": True, "features": {}}  # fmt: skip


async def _unsure_model(session, bar_end):
    return {**(await _confident_model(session, bar_end)), "p_up": 0.53, "confidence": 0.03, "trigger": False}


@needs_db
async def test_no_job_when_model_is_not_confident_even_with_signals(db, worker, monkeypatch):
    await seed_falling_bars(db, BAR_OPEN)
    freeze_worker_now(monkeypatch, BAR_OPEN + timedelta(seconds=60))
    monkeypatch.setattr("goldpulse.worker.__main__.model15m.evaluate", _unsure_model)
    await worker.signal_once(BAR_OPEN)
    assert await signal_jobs(db) == []  # 2 tín hiệu vẫn bật, nhưng luật mới do mô hình quyết (DECISIONS 2026-09-26)
    st = await signal_status(db)
    assert st["has_signal"] is True and st["model"]["trigger"] is False and st["job_id"] is None


@needs_db
async def test_signal_creates_exactly_one_prediction_job_even_if_run_twice_for_same_bar(db, worker, monkeypatch):
    await seed_falling_bars(db, BAR_OPEN)
    freeze_worker_now(monkeypatch, BAR_OPEN + timedelta(seconds=60))
    # Mô hình cần ~23 ngày lịch sử — ở đây chỉ kiểm hàng chờ: giả mô hình đang tự tin (trigger)
    monkeypatch.setattr("goldpulse.worker.__main__.model15m.evaluate", _confident_model)

    await worker.signal_once(BAR_OPEN)
    jobs = await signal_jobs(db)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["model"]["trigger"] is True
    assert jobs[0]["payload"]["bar_ts"] == BAR_OPEN.isoformat()
    assert jobs[0]["payload"]["signals"]["has_signal"] is True
    st = await signal_status(db)
    assert st["market_ok"] is True and st["job_id"] == jobs[0]["id"] and st["streak"] == -29

    # Worker khởi động lại giữa nến → chạy lại đúng mốc đó: không đẻ job thứ hai (sổ dự đoán không nhân đôi)
    await worker.signal_once(BAR_OPEN)
    assert len(await signal_jobs(db)) == 1


@needs_db
async def test_signal_in_last_15_minutes_before_daily_break_records_status_but_no_job(db, worker, monkeypatch):
    await seed_falling_bars(db, BAR_BEFORE_BREAK)
    freeze_worker_now(monkeypatch, BAR_BEFORE_BREAK + timedelta(seconds=60))

    await worker.signal_once(BAR_BEFORE_BREAK)
    assert await signal_jobs(db) == []
    st = await signal_status(db)
    assert st is not None, "S6 phải thấy tín hiệu mới nhất kể cả khi không được dự đoán"
    assert st["bar_end"] == BAR_BEFORE_BREAK.isoformat()
    assert st["has_signal"] is True and st["market_ok"] is False and st["job_id"] is None


@needs_db
async def test_signal_for_bar_older_than_5_minutes_makes_no_job(db, worker, monkeypatch):
    await seed_falling_bars(db, BAR_OPEN)
    freeze_worker_now(monkeypatch, BAR_OPEN + timedelta(minutes=6))  # worker khởi động muộn giữa nến

    await worker.signal_once(BAR_OPEN)
    assert await signal_jobs(db) == []
    st = await signal_status(db)
    assert st["has_signal"] is True and st["market_ok"] is False


# --- 2. Chấm dự đoán + tỉ lệ đúng (metric GO/KILL) -------------------------------------------------


async def _pred(scope, made: datetime, direction: str, price: float) -> int:
    from goldpulse.services import predictions

    async with scope() as s:
        return await predictions.create(
            s, made_at=made, source="chat", direction=direction, confidence="low", price=price,
            signals={}, reasoning="", backend="fake", model="fake", job_id=None,
        )  # fmt: skip


@needs_db
async def test_scoring_flat_wrong_and_candle_outside_5_minute_window(db):
    from goldpulse.repositories import predictions as pred_repo
    from goldpulse.services import predictions

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    # flat: |Δ| < 0,01. Nến ĐÚNG ở due_at không được dùng (chỉ nến < due_at)
    flat = await _pred(db, now - timedelta(minutes=20), "up", 4300.0)
    await insert_candle(db, now - timedelta(minutes=6), 4300.005)
    await insert_candle(db, now - timedelta(minutes=5), 5000.0)
    # wrong: đoán lên, giá xuống
    wrong = await _pred(db, now - timedelta(minutes=60), "up", 4300.0)
    await insert_candle(db, now - timedelta(minutes=46), 4299.0)
    # unscored: nến gần nhất cách due_at 6 phút — ngoài cửa sổ 5 phút
    gap = await _pred(db, now - timedelta(minutes=120), "down", 4300.0)
    await insert_candle(db, now - timedelta(minutes=111), 4200.0)
    # chưa tới hạn + 90s → giữ pending
    fresh = await _pred(db, now - timedelta(minutes=14), "up", 4300.0)

    async with db() as s:
        assert await predictions.score_due(s) == 3
    async with db() as s:
        got = {pid: await pred_repo.get(s, pid) for pid in (flat, wrong, gap, fresh)}
    assert got[flat]["outcome"] == "flat" and got[flat]["price_due"] == 4300.005
    assert got[wrong]["outcome"] == "wrong" and got[wrong]["price_due"] == 4299.0
    assert got[gap]["outcome"] == "unscored" and got[gap]["price_due"] is None
    assert got[fresh]["outcome"] == "pending" and got[fresh]["scored_at"] is None


async def _bulk(scope, n: int, source: str, direction: str, outcome: str, offset: int) -> None:
    async with scope() as s:
        await s.execute(
            text("""INSERT INTO prediction (made_at, source, direction, price_at, due_at, outcome)
                    SELECT now() - make_interval(mins => g + :off), :src, :dir, 4300, now(), :o
                    FROM generate_series(1, :n) g"""),
            {"n": n, "src": source, "dir": direction, "o": outcome, "off": offset},
        )


@needs_db
async def test_accuracy_counts_only_correct_and_wrong_and_waits_for_100_scored(db):
    from goldpulse.services import predictions

    await _bulk(db, 3, "auto", "up", "correct", 0)
    await _bulk(db, 1, "chat", "down", "wrong", 100)
    await _bulk(db, 5, "chat", "up", "flat", 200)  # flat / unscored / pending KHÔNG kéo tỉ lệ xuống
    await _bulk(db, 4, "auto", "down", "unscored", 300)
    await _bulk(db, 2, "chat", "up", "pending", 400)
    async with db() as s:
        sm = await predictions.summary(s)
    assert (sm["correct"], sm["wrong"], sm["scored"]) == (3, 1, 4)
    assert sm["rate"] == 75.0
    assert (sm["flat"], sm["unscored"], sm["pending"], sm["total"]) == (5, 4, 2, 15)
    assert sm["verdict"] == "pending"  # 75% nhưng mới 4 lần chấm — chưa được kết luận GO
    assert sm["by_source"]["auto"]["rate"] == 100.0 and sm["by_source"]["chat"]["rate"] == 0.0

    # Đủ 100 lần chấm: 55 đúng / 45 sai = 55% → go (flat vẫn không tính)
    await _bulk(db, 52, "chat", "up", "correct", 1000)
    await _bulk(db, 44, "chat", "down", "wrong", 2000)
    async with db() as s:
        sm = await predictions.summary(s)
    assert sm["scored"] == 100 and sm["rate"] == 55.0 and sm["verdict"] == "go"


# --- 3. Não xử job dự đoán tự động -----------------------------------------------------------------


async def _open_live(session, now):
    return {
        "price": 4300.0,
        "price_time_vn": "00:00 26/09",
        "lag_seconds": 1,
        "fresh": True,
        "market_open": True,
        "can_suggest": True,
        "no_suggest_reason": None,
        "next_open_vn": None,
        "lag_limit": 60,
    }


async def _enqueue_and_claim(scope, bar_end: datetime, age: timedelta = timedelta(0)) -> dict:
    from goldpulse.repositories import jobs as job_repo

    async with scope() as s:
        jid = await job_repo.enqueue(
            s, "signal_prediction", {"bar_ts": bar_end.isoformat(), "signals": {"hint": "up", "has_signal": True}}
        )
        if age:
            await s.execute(
                text("UPDATE bot_job SET created_at = :t WHERE id = :i"), {"t": datetime.now(UTC) - age, "i": jid}
            )
    async with scope() as s:
        job = await job_repo.claim(s)
    assert job and job["id"] == jid
    return job


async def _job_and_preds(scope, jid: int):
    async with scope() as s:
        job = (await s.execute(text("SELECT status, last_error FROM bot_job WHERE id = :i"), {"i": jid})).one()
        preds = (await s.execute(text("SELECT source, job_id FROM prediction"))).all()
    return job, preds


@needs_db
async def test_brain_drops_prediction_job_older_than_5_minutes_without_marking_brain_broken(db):
    from goldpulse.brain.__main__ import BrainLoop
    from goldpulse.integrations.claude_fake import ClaudeFake

    job = await _enqueue_and_claim(db, BAR_OPEN, age=timedelta(minutes=6))
    brain = BrainLoop(ClaudeFake(delay=0))
    assert await brain.handle(job) is True  # bỏ hẳn, không thử lại
    status, preds = await _job_and_preds(db, job["id"])
    assert status.status == "error" and "5 phút" in status.last_error
    assert preds == []  # giá đã trôi — không ghi dự đoán vào sổ
    assert brain.last_error is None  # bỏ nến vì điều kiện ≠ não hỏng — huy hiệu không đỏ


@needs_db
async def test_brain_valid_prediction_job_writes_one_auto_prediction_and_finishes_job(db, monkeypatch):
    from goldpulse.brain.__main__ import BrainLoop
    from goldpulse.integrations.claude_fake import ClaudeFake

    monkeypatch.setattr("goldpulse.brain.__main__.context_pack.live_state", _open_live)
    now = datetime.now(UTC)
    for i in range(1, 4):  # nến phút gần nhất để lấy giá lúc dự đoán
        await insert_candle(db, (now - timedelta(minutes=i)).replace(second=0, microsecond=0), 4300.0 + i)
    job = await _enqueue_and_claim(db, BAR_OPEN)
    brain = BrainLoop(ClaudeFake(delay=0))
    assert await brain.handle(job) is True

    status, preds = await _job_and_preds(db, job["id"])
    assert status.status == "done" and status.last_error is None
    assert [(p.source, p.job_id) for p in preds] == [("auto", job["id"])]
    async with db() as s:
        p = (await s.execute(text("SELECT direction, price_at, due_at - made_at AS h FROM prediction"))).one()
    assert p.direction == "up" and p.price_at == 4301.0 and p.h == timedelta(minutes=15)
    assert brain.last_error is None


# --- 4. Nạp FED idempotent ---------------------------------------------------------------------------


@needs_db
async def test_fed_reload_twice_does_not_duplicate_rows(db):
    from goldpulse.repositories import fed as fed_repo
    from goldpulse.services import fed

    pts = []
    d = date(2019, 6, 1)
    while d <= date(2020, 6, 1):
        upper = (
            2.5 if d < date(2019, 8, 1) else 2.25 if d < date(2020, 3, 4) else 1.25 if d < date(2020, 3, 16) else 0.25
        )
        pts.append(RatePoint(d, upper - 0.25, upper))
        d += timedelta(days=1)
    rows = fed.build_decisions(pts)
    assert rows and len({r["meeting_date"] for r in rows}) == len(rows)

    async with db() as s:
        await fed_repo.upsert_many(s, rows)
    async with db() as s:
        first = await fed_repo.count(s)
    async with db() as s:  # nạp lại hằng ngày 06:00 UTC
        await fed_repo.upsert_many(s, fed.build_decisions(pts))
    async with db() as s:
        assert await fed_repo.count(s) == first == len(rows)
        total_bp = (await s.execute(text("SELECT sum(change_bp) FROM fed_decision"))).scalar()
    assert total_bp == -225  # 2,50 → 0,25: không kỳ nào bị đếm hai lần


# --- 5. Gói dữ liệu chat cho não ---------------------------------------------------------------------


@needs_db
async def test_chat_pack_has_all_sections_history_is_ok_only_before_answer_max_20_and_serializable(db):
    from goldpulse.repositories import chat as chat_repo
    from goldpulse.repositories import fed as fed_repo
    from goldpulse.services import context_pack, fed

    now = datetime.now(UTC)
    ann = fed.announced_at(date(2024, 9, 18))
    async with db() as s:
        await fed_repo.upsert_many(
            s,
            [{"meeting_date": date(2024, 9, 18), "announced_at": ann, "rate_lower": 4.75, "rate_upper": 5.0,
              "change_bp": -50, "kind": "scheduled", "source_note": "t"}],
        )  # fmt: skip
        await s.execute(
            text("""INSERT INTO feed_sample (polled_at, source_ts, price, lag_seconds, ok)
                    VALUES (:n, :n, 4300, 1, true)"""),
            {"n": now},
        )
        tid = await chat_repo.create_thread(s, "t")
        other = await chat_repo.create_thread(s, "khác")
        await chat_repo.add_message(s, other, "user", "thread khác — không được lẫn vào", "ok")
        for i in range(15):  # 30 tin ok cũ
            await chat_repo.add_message(s, tid, "user", f"hỏi {i}", "ok")
            await chat_repo.add_message(s, tid, "assistant", f"đáp {i}", "ok")
        await chat_repo.add_message(s, tid, "user", "hỏi lỗi", "ok")
        await chat_repo.add_message(s, tid, "assistant", "LỖI không được vào lịch sử", "error")
        await chat_repo.add_message(s, tid, "user", "câu hỏi hiện tại", "ok")
        answer = await chat_repo.add_message(s, tid, "assistant", "", "pending")

    async with db() as s:
        pack = await context_pack.build_chat(s, tid, answer, now)

    for key in ("live", "signals_15m", "fed", "bot_accuracy", "conversation"):
        assert key in pack, key
    conv = pack["conversation"]
    assert 0 < len(conv) <= 20
    contents = [m["content"] for m in conv]
    assert contents[-1] == "câu hỏi hiện tại"  # lịch sử là đoạn GẦN NHẤT, không phải đầu thread
    assert "LỖI không được vào lịch sử" not in contents
    assert "" not in contents  # câu trả lời đang chờ không tự nằm trong lịch sử
    assert "thread khác — không được lẫn vào" not in contents
    assert all(m["role"] in ("user", "assistant") for m in conv)
    assert pack["fed"]["recent"] and pack["fed"]["recent"][0]["change_bp"] == -50
    json.dumps(pack, ensure_ascii=False)  # không default=str — date/Decimal/datetime lọt vào gói là vỡ ở đây
