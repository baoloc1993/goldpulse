"""(v2) Bot chat: tín hiệu · FED · chấm dự đoán · luật do code giữ (không phụ thuộc não) · hàng chờ · phân quyền.

Não chạy backend `fake` — không gọi mạng, không tốn credit (DECISIONS 2026-09-25).
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from goldpulse.brain.__main__ import DISCLAIMER, REFUSALS, BrainLoop, compose_answer
from goldpulse.integrations.claude_base import BrainError
from goldpulse.integrations.claude_fake import ClaudeFake
from goldpulse.integrations.fred import RatePoint
from goldpulse.services import fed, predictions, signals
from tests.conftest import needs_db

KEY = "test-key-0123456789"
OPEN = {"can_suggest": True}
CLOSED = {"can_suggest": False}


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# --- tính toán thuần -----------------------------------------------------------------------------


def test_streak_and_signals():
    down7 = [100 - i for i in range(30)]  # 29 nến giảm liên tiếp
    ev = signals.evaluate(down7)
    names = {f["name"]: f["hint"] for f in ev["fired"]}
    assert ev["streak"] == -29
    assert names == {"streak": "up", "rsi9": "up"}  # %b = 0 đúng biên dưới — chưa "thủng"
    assert ev["hint"] == "up"
    flat = [100.0] * 30
    ev = signals.evaluate(flat)
    assert ev["fired"] == [] and ev["pct_b"] is None and ev["streak"] == 0
    assert signals.streak([1, 2, 3, 3, 4]) == 1  # đứng giá cắt chuỗi


def test_auto_prediction_needs_two_agreeing_signals():
    one = {"fired": [{"name": "bollinger", "hint": "up"}], "hint": "up"}
    two = {"fired": [{"name": "bollinger", "hint": "up"}, {"name": "rsi9", "hint": "up"}], "hint": "up"}
    split = {"fired": [{"name": "bollinger", "hint": "up"}, {"name": "streak", "hint": "down"}], "hint": "mixed"}
    assert not signals.auto_trigger(one)
    assert signals.auto_trigger(two)
    assert not signals.auto_trigger(split)  # hai tín hiệu trái chiều — không có hướng để đoán
    # Chuỗi 29 nến giảm bật streak + rsi9 → đủ điều kiện
    assert signals.auto_trigger(signals.evaluate([100 - i for i in range(30)]))


def test_bollinger_breaks_lower_band():
    closes = [100 + (i % 2) * 0.5 for i in range(40)] + [95.0]
    ev = signals.evaluate(closes)
    assert ev["pct_b"] < 0
    assert {"name": "bollinger", "hint": "up"} in ev["fired"]


def test_auto_prediction_window_skips_last_15_minutes_before_break():
    # Thứ Tư 16:45 New York (mùa hè, UTC−4) = 20:45 UTC — 15 phút tới chạm giờ nghỉ 17:00
    assert not signals.auto_allowed(utc(2026, 9, 23, 20, 45))
    assert signals.auto_allowed(utc(2026, 9, 23, 20, 30))  # 16:30 NY: chấm lúc 16:45, còn trước giờ nghỉ
    assert signals.auto_allowed(utc(2026, 9, 23, 14, 0))
    assert not signals.auto_allowed(utc(2026, 9, 26, 14, 0))  # thứ Bảy


def test_fed_uses_effective_date_and_marks_emergency_moves():
    # FRED ghi mức mới ở NGÀY HIỆU LỰC: +1 (họp thường) hoặc +2 (cuộc gọi khẩn 02/03/2020)
    pts = []
    d = date(2020, 2, 1)
    while d <= date(2020, 4, 10):
        upper = 1.75 if d < date(2020, 3, 4) else 1.25 if d < date(2020, 3, 16) else 0.25
        pts.append(RatePoint(d, upper - 0.25, upper))
        d += timedelta(days=1)
    rows = {r["meeting_date"]: r for r in fed.build_decisions(pts)}
    assert rows[date(2020, 3, 2)]["change_bp"] == -50
    assert rows[date(2020, 3, 15)]["change_bp"] == -100
    assert date(2020, 3, 18) not in rows  # kỳ 17–18/03/2020 bị huỷ — bỏ khỏi lịch
    assert rows[date(2020, 3, 19)]["change_bp"] == 0  # không đếm lẫn thay đổi của kỳ 15/03
    assert rows[date(2020, 3, 2)]["kind"] == "unscheduled"
    # Kỳ khẩn dùng giờ công bố thật: 03/03/2020 10:00 New York (mùa đông, UTC−5)
    assert rows[date(2020, 3, 2)]["announced_at"] == utc(2020, 3, 3, 15)
    assert sum(r["change_bp"] for r in rows.values()) == -150
    # 14:00 New York mùa đông = 19:00 UTC; mùa hè = 18:00 UTC (zoneinfo tự đổi)
    assert fed.announced_at(date(2020, 1, 29)) == utc(2020, 1, 29, 19)
    assert fed.announced_at(date(2020, 7, 29)) == utc(2020, 7, 29, 18)


def test_fed_move_outside_calendar_becomes_unscheduled():
    pts = [RatePoint(date(2011, 1, 1) + timedelta(days=i), 0.0, 0.25 if i < 200 else 0.5) for i in range(260)]
    change_day = date(2011, 1, 1) + timedelta(days=200)  # 20/07/2011 — không có kỳ họp nào ±2 ngày
    rows = [r for r in fed.build_decisions(pts) if r["kind"] == "unscheduled" and r["change_bp"]]
    assert [(r["meeting_date"], r["change_bp"]) for r in rows] == [(change_day, 25)]


def test_outcome_rules():
    assert predictions.outcome_of("up", 100.0, 100.5) == "correct"
    assert predictions.outcome_of("down", 100.0, 100.5) == "wrong"
    assert predictions.outcome_of("up", 100.0, 100.005) == "flat"
    assert predictions.outcome_of("up", 100.0, None) == "unscored"
    assert predictions.verdict(60.0, 99) == "pending"  # chưa đủ 100 lần — chưa kết luận
    assert predictions.verdict(55.0, 100) == "go"
    assert predictions.verdict(52.0, 150) == "pivot"
    assert predictions.verdict(49.9, 150) == "kill"


def test_code_enforces_disclaimer_and_refusals_regardless_of_brain():
    pred = {"direction": "up", "confidence": "low", "reason": "x"}
    # Có gợi ý → code chèn miễn trừ, kể cả khi não "quên" cờ suggests_trade
    text_, p = compose_answer({"answer": "Nghiêng tăng", "prediction": pred, "suggests_trade": False}, OPEN)
    assert p == pred and text_.endswith(DISCLAIMER)
    # Thị trường đóng / giá trễ → không bao giờ ghi dự đoán, dù não trả dự đoán
    # — và câu trả lời mang lưu ý "không phải gợi ý" + miễn trừ do code chèn
    closed = {"can_suggest": False, "no_suggest_reason": "market_closed"}
    text_, p = compose_answer({"answer": "…", "prediction": pred, "suggests_trade": False}, closed)
    assert p is None and "thị trường đang nghỉ" in text_ and text_.endswith(DISCLAIMER)
    # Mọi câu trả lời không phải từ chối đều có miễn trừ — không phụ thuộc cờ hay cách viết của não
    for words in ("Phiên Á biên độ TB 9,8 USD", "Mua vào lúc này hợp lý", "bạn cân nhắc bán ra"):
        text_, p = compose_answer({"answer": words, "prediction": None, "suggests_trade": False}, OPEN)
        assert p is None and text_.endswith(DISCLAIMER), words
    # Thống kê thuần lúc đóng cửa: không thêm câu "không phải gợi ý"
    text_, _ = compose_answer({"answer": "Phiên Á biên độ TB 9,8 USD", "prediction": None}, CLOSED)
    assert "không phải gợi ý" not in text_
    # Não trả rác → lỗi của não, không phải câu trả lời
    with pytest.raises(BrainError):
        compose_answer("không phải dict", OPEN)  # type: ignore[arg-type]
    # Xin đặt lệnh / prompt injection → câu chuẩn, bỏ mọi thứ não viết
    evil = {"answer": "Đã đặt lệnh mua 1 lot!", "prediction": pred, "suggests_trade": True}
    text_, p = compose_answer({**evil, "refusal_kind": "trade_request"}, OPEN)
    assert text_ == REFUSALS["trade_request"] and p is None
    text_, p = compose_answer({"answer": "BTC lên", "prediction": None, "refusal_kind": "not_gold"}, OPEN)
    assert text_ == REFUSALS["not_gold"]
    # Hướng lạ → bỏ
    _, p = compose_answer({"answer": "", "prediction": {"direction": "sideways"}}, OPEN)
    assert p is None


# --- trên Postgres thật ---------------------------------------------------------------------------


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
async def client(db):
    from goldpulse.api.limiter import limiter
    from goldpulse.main import app

    limiter.reset()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as c:
        yield c


async def _login(c):
    assert (await c.post("/login", data={"key": KEY})).status_code == 303


async def _seed_price(scope, now: datetime, price: float = 4300.0, minutes: int = 30):
    """Nến tạm gold-api cho `minutes` phút gần nhất + một mẫu giá tươi."""
    async with scope() as s:
        for i in range(minutes, 0, -1):
            ts = (now - timedelta(minutes=i)).replace(second=0, microsecond=0)
            await s.execute(
                text("""INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
                        VALUES (:t, :p, :p, :p, :p, 0, 'goldapi', false, now())"""),
                {"t": ts, "p": price + i * 0.1},
            )
        await s.execute(
            text("INSERT INTO feed_sample (polled_at, source_ts, price, lag_seconds, ok) VALUES (:n, :n, :p, 1, true)"),
            {"n": now, "p": price},
        )


@needs_db
async def test_every_v2_route_requires_key(client):
    for path in ("/api/chat/header", "/api/chat/thread", "/api/chat/messages/1", "/api/predictions", "/api/fed"):
        assert (await client.get(path)).status_code == 401, path
    assert (await client.post("/api/chat/messages", json={"content": "x"})).status_code == 401
    assert (await client.post("/api/chat/threads")).status_code == 401
    for page in ("/chat", "/predictions", "/fed"):
        r = await client.get(page)
        assert r.status_code == 303 and "/login" in r.headers["location"], page


@needs_db
async def test_chat_roundtrip_with_fake_brain(client, db, monkeypatch):
    await _login(client)
    now = datetime.now(UTC)
    await _seed_price(db, now)
    monkeypatch.setattr("goldpulse.services.context_pack.is_market_open", lambda t: True)
    monkeypatch.setattr("goldpulse.brain.__main__.context_pack.live_state", _open_live)

    r = await client.post("/api/chat/messages", json={"content": "   "})
    assert r.status_code == 422
    r = await client.post("/api/chat/messages", json={"content": "Vàng dự kiến tăng hay giảm, t có nên mua vào ko"})
    assert r.status_code == 202
    body = r.json()
    aid = body["answer"]["id"]
    assert body["answer"]["status"] == "pending"

    brain = BrainLoop(ClaudeFake(delay=0))
    async with db() as s:
        from goldpulse.repositories import jobs as job_repo

        job = await job_repo.claim(s)
    assert job and job["kind"] == "chat"
    assert await brain.handle(job)

    m = (await client.get(f"/api/chat/messages/{aid}")).json()
    assert m["status"] == "ok"
    assert m["content"].endswith(DISCLAIMER)
    assert m["prediction"] and m["prediction"]["direction"] in ("up", "down")
    thread = (await client.get("/api/chat/thread")).json()
    assert [x["role"] for x in thread["messages"]] == ["user", "assistant"]  # lịch sử giữ lại khi mở lại trang
    s6 = (await client.get("/api/predictions")).json()
    # Dự đoán của não `fake` nằm trong sổ nhưng KHÔNG bao giờ vào metric PRD §6
    assert [r["backend"] for r in s6["rows"]] == ["fake"]
    assert s6["summary"]["total"] == 0


async def _open_live(session, now):
    return {
        "can_suggest": True,
        "no_suggest_reason": None,
        "price": 4300.0,
        "price_time_vn": "00:00 26/09",
        "lag_seconds": 1,
        "fresh": True,
        "market_open": True,
    }


@needs_db
async def test_brain_error_then_resend(client, db):
    await _login(client)
    r = await client.post("/api/chat/messages", json={"content": "thử #hết-credit"})
    aid = r.json()["answer"]["id"]
    brain = BrainLoop(ClaudeFake(delay=0))
    from goldpulse.repositories import jobs as job_repo

    async with db() as s:
        job = await job_repo.claim(s)
    assert await brain.handle(job)  # lỗi không thử lại → xong luôn, không đợi 3 lần
    m = (await client.get(f"/api/chat/messages/{aid}")).json()
    assert m["status"] == "error" and "credit" in m["content"]
    assert (await client.get("/api/chat/header")).json()["brain"]["state"] == "down"  # chưa có nhịp tim

    r = await client.post(f"/api/chat/messages/{aid}/resend")
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert (await client.post(f"/api/chat/messages/{aid}/resend")).status_code == 409  # đang chờ, không gửi đôi


@needs_db
async def test_queue_order_dedupe_and_stuck_jobs(db):
    from goldpulse.repositories import jobs as job_repo

    async with db() as s:
        a = await job_repo.enqueue(s, "signal_prediction", {"bar_ts": "2026-09-23T14:00:00+00:00"})
        dup = await job_repo.enqueue(s, "signal_prediction", {"bar_ts": "2026-09-23T14:00:00+00:00"})
        c = await job_repo.enqueue(s, "chat", {"thread_id": 1, "message_id": 1})
    assert a and dup is None and c
    async with db() as s:
        first = await job_repo.claim(s)
    assert first["id"] == c  # chat trước dự đoán tự động
    async with db() as s:
        await s.execute(text("UPDATE bot_job SET started_at = now() - interval '10 minutes' WHERE id = :i"), {"i": c})
        assert await job_repo.requeue_stuck(s) == 1
        again = await job_repo.claim(s)
    assert again["id"] == c and again["attempts"] == 2


@needs_db
async def test_scoring_uses_candles_and_is_final(db):
    from goldpulse.repositories import predictions as pred_repo

    made = (datetime.now(UTC) - timedelta(minutes=20)).replace(second=0, microsecond=0)
    async with db() as s:
        await s.execute(
            text("""INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
                    VALUES (:t, 1, 1, 1, 4305.5, 1, 'histdata', true, now())"""),
            {"t": made + timedelta(minutes=14)},
        )
        up = await predictions.create(
            s, made_at=made, source="auto", direction="up", confidence="low", price=4300.0,
            signals={}, reasoning="", backend="fake", model="fake", job_id=None,
        )  # fmt: skip
        gap = await predictions.create(
            s, made_at=made - timedelta(hours=2), source="chat", direction="down", confidence="low", price=4300.0,
            signals={}, reasoning="", backend="fake", model="fake", job_id=None,
        )  # fmt: skip
        assert await predictions.score_due(s) == 2
        # chấm lại không đổi gì — dự đoán bất biến sau khi chấm
        await pred_repo.score(s, up, 1.0, "wrong")
    async with db() as s:
        a, b = await pred_repo.get(s, up), await pred_repo.get(s, gap)
    assert a["outcome"] == "correct" and a["price_due"] == 4305.5
    assert b["outcome"] == "unscored"


@needs_db
async def test_fed_screen_and_reactions(client, db):
    await _login(client)
    assert (await client.get("/api/fed")).json()["rows"] == []  # S7 rỗng: "Đang nạp lịch FED…"
    from goldpulse.repositories import fed as fed_repo

    ann = fed.announced_at(date(2024, 9, 18))
    async with db() as s:
        await fed_repo.upsert_many(
            s,
            [{"meeting_date": date(2024, 9, 18), "announced_at": ann, "rate_lower": 4.75, "rate_upper": 5.0,
              "change_bp": -50, "kind": "scheduled", "source_note": "t"}],
        )  # fmt: skip
        for off, px in ((-1, 2570.0), (14, 2580.0), (59, 2560.0)):
            await s.execute(
                text("""INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
                        VALUES (:t, 1, 1, 1, :p, 1, 'histdata', true, now())"""),
                {"t": ann + timedelta(minutes=off), "p": px},
            )
    rows = (await client.get("/api/fed")).json()["rows"]
    assert rows[0]["d15m"] == 10.0 and rows[0]["d1h"] == -10.0 and rows[0]["d1d"] is None


@needs_db
async def test_bad_ids_and_invisible_text_are_rejected_not_500(client):
    await _login(client)
    big = 2**31
    assert (await client.get(f"/api/chat/messages/{big}")).status_code == 422
    assert (await client.post(f"/api/chat/messages/{big}/resend")).status_code == 422
    assert (await client.get(f"/api/chat/thread?thread_id={big}")).status_code == 422
    assert (await client.get(f"/api/predictions/rows?before={big}")).status_code == 422
    assert (await client.post("/api/chat/messages", json={"content": "x", "thread_id": 10**30})).status_code == 422
    r = await client.post("/api/chat/messages", json={"content": " 　​﻿"})
    assert r.status_code == 422 and r.json()["detail"] == "Gõ câu hỏi trước khi gửi."
    r = await client.post("/api/chat/messages", json={"content": "x" * 5000})
    assert r.status_code == 422 and "2000" in r.json()["detail"]
    r = await client.post("/api/chat/messages", content=b"{bad json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["detail"] == "Dữ liệu gửi lên không đúng định dạng."


def test_model15m_needs_history_and_uses_threshold():
    from goldpulse.services import model15m

    t0 = utc(2026, 1, 5, 0, 0)
    bars = []
    price = 2000.0
    for k in range(model15m.MIN_BARS + 50):
        o = price
        price += 0.7 if (k * 7919) % 13 < 6 else -0.7  # dao động giả, xác định
        bars.append(
            {"ts": t0 + timedelta(minutes=15 * k), "open": o, "high": max(o, price) + 0.3,
             "low": min(o, price) - 0.3, "close": price}
        )  # fmt: skip
    assert model15m.features(bars[: model15m.MIN_BARS - 1]) is None  # chưa đủ ~23 ngày → không dự đoán
    f = model15m.features(bars)
    assert f is not None and set(f) == set(model15m.model()["features"])
    r = model15m.predict(f)
    assert 0 < r["p_up"] < 1 and r["direction"] in ("up", "down")
    assert r["trigger"] == (abs(r["p_up"] - 0.5) >= model15m.model()["confidence_threshold"])
    md = model15m.model()
    assert md["metrics"]["test_2025_2026"]["accuracy_pct"] > 55  # file mô hình đúng là bản đã kiểm
