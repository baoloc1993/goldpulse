"""(v3) Tài khoản giấy — luồng tiền/dữ liệu chưa có ở test_paper.py: luật D 60 phút, ký quỹ, ngày mới, worker vắng,
verdict từ sổ thật, tắt khi đang có lệnh, số dư tạm qua API, phân trang, gói dữ liệu cho não. Tiền giả.

Mọi kịch bản truyền `now` tường minh quanh T0 = thứ Tư 23/09/2026 10:00 New York (thị trường mở).
"""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from goldpulse.repositories import feed as feed_repo
from goldpulse.repositories import paper as repo
from goldpulse.services import paper
from tests.conftest import needs_db
from tests.test_paper import KEY, T0, _candle, _pred

M = timedelta(minutes=1)
DAY = date(2026, 9, 23)


@pytest.fixture
async def db():
    from goldpulse.db import engine, session_scope

    async with session_scope() as s:
        await s.execute(
            text(
                "TRUNCATE paper_trade, paper_day, bot_job, prediction, candle_1m, feed_sample, worker_status"
                " RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(text("UPDATE paper_account SET enabled = true"))
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


async def _trades(s, code: str) -> list[dict]:
    """Mọi dòng của một tài khoản, id tăng dần."""
    return list(reversed(await repo.trades_page(s, code, None, 100)))


async def _win_cycle(s, start: datetime, exit_price: float = 4302.0) -> None:
    """Một dự đoán lên ở 4300 lúc `start`, nến phút cuối trước hạn 15' ở `exit_price`, đóng lúc hạn + 2'."""
    await _pred(s, start)
    await paper.open_for_new(s, start + timedelta(seconds=30))
    await _candle(s, start + 14 * M, exit_price)
    await paper.close_due(s, start + 17 * M)


# --- luật D, ký quỹ ------------------------------------------------------------------------------


@needs_db
async def test_rule_d_holds_60_minutes_closes_at_due_price_and_skips_while_open(db):
    async with db() as s:
        await _win_cycle(s, T0)  # A, C đóng ở 14:15; D (60') còn mở
        assert (await repo.open_trade_of(s, "D")) is not None
        await _pred(s, T0 + 20 * M)
        await paper.open_for_new(s, T0 + 20 * M + timedelta(seconds=30))
        a2, d2 = (await _trades(s, "A"))[-1], (await _trades(s, "D"))[-1]
        assert a2["status"] == "open"  # A đã đóng lệnh trước → mở được
        assert d2["status"] == "skipped" and d2["skip_reason"] == "open_trade"
        await _candle(s, T0 + 34 * M, 4301.0)  # giá ra lệnh A thứ hai (hạn 14:35)
        await _candle(s, T0 + 59 * M, 4305.0)  # nến phút cuối trước hạn D 15:00
        await _candle(s, T0 + 60 * M, 4400.0)  # nến của phút chứa hạn — KHÔNG được dùng
        await paper.close_due(s, T0 + 61 * M + timedelta(seconds=30))
    async with db() as s:
        d = (await _trades(s, "D"))[0]
        day_d = await repo.day_get(s, "D", DAY)
    assert d["status"] == "closed" and d["exit_reason"] == "horizon"
    assert d["exit_price"] == 4305.0 and d["pnl"] == pytest.approx(4.7)  # (4305 − 4300) − 0,30
    assert d["closed_at"] == T0 + 60 * M  # đóng tại hạn, không tại giờ worker chạy
    assert day_d["n_trades"] == 1 and day_d["end_equity"] == pytest.approx(14.7)


@needs_db
async def test_balance_below_margin_after_loss_skips_next_trade(db):
    async with db() as s:
        # lệnh mua 4300 → ra 4294: −6 − 0,30 = −6,30 → còn 3,70 < 4300/1000 = 4,30
        await _win_cycle(s, T0, exit_price=4294.0)
        day = await repo.day_get(s, "A", DAY)
        assert day["end_equity"] == pytest.approx(3.7) and not day["burned"]
        await _pred(s, T0 + 20 * M)
        await paper.open_for_new(s, T0 + 20 * M + timedelta(seconds=30))
        a = (await _trades(s, "A"))[-1]
        assert await repo.open_trade_of(s, "A") is None
    assert a["status"] == "skipped" and a["skip_reason"] == "margin"


# --- ngày mới -------------------------------------------------------------------------------------


@needs_db
async def test_account_burned_yesterday_reopens_with_10_usd_after_18h_new_york(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        await _candle(s, T0 + 3 * M, 4290.0)
        await paper.close_due(s, T0 + 4 * M)
        assert (await repo.day_get(s, "A", DAY))["burned"]

        t1 = datetime(2026, 9, 23, 22, 30, tzinfo=UTC)  # 18:30 NY → ngày giao dịch 24/09
        await _candle(s, t1 - M, 4300.0)
        await _pred(s, t1)
        await paper.open_for_new(s, t1 + timedelta(seconds=30))
        a = (await _trades(s, "A"))[-1]
        new_day = await repo.day_get(s, "A", date(2026, 9, 24))
        old_day = await repo.day_get(s, "A", DAY)
        acc = await repo.get_account(s, "A")
        view = await paper.account_view(s, acc, t1 + M, 4300.0)
    assert a["status"] == "open" and a["trade_date"] == date(2026, 9, 24)
    assert new_day["start_equity"] == 10.0 and new_day["realized_pnl"] == 0 and not new_day["burned"]
    assert old_day["burned"] and old_day["end_equity"] == 0  # sổ hôm qua giữ nguyên
    assert view["today"]["burned"] is False and view["equity"] == pytest.approx(9.7)  # 10 − spread
    assert view["total"]["days"] == 2 and view["total"]["burned_days"] == 1


# --- worker vắng ----------------------------------------------------------------------------------


@needs_db
async def test_worker_back_late_closes_at_candle_before_due_and_no_candle_is_market_close_out_of_eval(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        await _candle(s, T0 + 14 * M, 4302.0)  # nến phút cuối trước hạn 14:15
        await _candle(s, T0 + 40 * M, 4350.0)  # giá "hiện tại" khi worker sống lại
        await paper.close_due(s, T0 + 45 * M)
        a1 = (await _trades(s, "A"))[0]
        assert a1["exit_reason"] == "horizon" and a1["exit_price"] == 4302.0
        assert a1["pnl"] == pytest.approx(1.7) and a1["closed_at"] == T0 + 15 * M

        # lệnh 2 hạn 15:05, không có nến nào trong [15:00, 15:05) → market_close ở nến cuối có
        await _pred(s, T0 + 50 * M)
        await paper.open_for_new(s, T0 + 50 * M + timedelta(seconds=30))
        await paper.close_due(s, T0 + 80 * M)
        a2 = (await _trades(s, "A"))[-1]
        tot = await repo.totals(s, "A")
    assert a2["status"] == "closed" and a2["exit_reason"] == "market_close" and a2["exit_price"] == 4350.0
    assert tot["trades"] == 2 and tot["pnl"] == pytest.approx(1.7 + 49.7)  # vẫn tính vào số dư ngày
    assert tot["eval_n"] == 1 and tot["eval_pnl"] == pytest.approx(1.7)  # nhưng loại khỏi GO/PIVOT/KILL


@needs_db
async def test_lost_cursor_and_double_close_do_not_duplicate_trades_or_day_totals(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        # worker_status mất mốc (khởi động lại, ghi đè) → đọc lại từ đầu
        await feed_repo.set_status(s, paper.STATUS_KEY, {"last_prediction_id": 0})
        assert await paper.open_for_new(s, T0 + timedelta(seconds=60)) == 0
        await _candle(s, T0 + 14 * M, 4302.0)
        await paper.close_due(s, T0 + 17 * M)
        await paper.close_due(s, T0 + 17 * M)  # hai nhịp cùng lúc / chạy lại
        n = (await s.execute(text("SELECT count(*) FROM paper_trade WHERE account_code = 'A'"))).scalar()
        day = await repo.day_get(s, "A", DAY)
    assert n == 1
    assert day["n_trades"] == 1 and day["realized_pnl"] == pytest.approx(1.7) and day["end_equity"] == 11.7


# --- verdict từ sổ thật ---------------------------------------------------------------------------


@needs_db
async def test_verdict_from_real_ledger_three_wins_is_pivot(db):
    async with db() as s:
        for i in range(3):
            await _win_cycle(s, T0 + 20 * i * M)
        acc = await repo.get_account(s, "A")
        view = await paper.account_view(s, acc, T0 + 60 * M, None)
    tot = view["total"]
    assert tot["eval_n"] == 3 and tot["eval_pnl"] == pytest.approx(5.1) and tot["eval_wins"] == 3
    assert tot["days"] == 1 and tot["burned_days"] == 0
    assert tot["verdict"] == "pivot"  # lãi nhưng chưa đủ 40 lệnh — không được "go"


# --- tắt tài khoản khi đang có lệnh ---------------------------------------------------------------


@needs_db
async def test_disable_with_open_trade_still_closes_at_horizon_and_reenable_keeps_day_balance(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        assert await paper.set_enabled(s, "A", False)
        await _candle(s, T0 + 14 * M, 4302.0)
        await paper.close_due(s, T0 + 17 * M)
        a1 = (await _trades(s, "A"))[0]
        assert a1["status"] == "closed" and a1["exit_reason"] == "horizon" and a1["pnl"] == pytest.approx(1.7)

        await _pred(s, T0 + 20 * M)
        await paper.open_for_new(s, T0 + 20 * M + timedelta(seconds=30))
        a2 = (await _trades(s, "A"))[-1]
        assert a2["status"] == "skipped" and a2["skip_reason"] == "disabled"

        assert await paper.set_enabled(s, "A", True)
        await _pred(s, T0 + 40 * M)
        await paper.open_for_new(s, T0 + 40 * M + timedelta(seconds=30))
        a3 = (await _trades(s, "A"))[-1]
        day = await repo.day_get(s, "A", DAY)
    assert a3["status"] == "open"
    assert day["start_equity"] == 10.0 and day["realized_pnl"] == pytest.approx(1.7)  # bật lại không reset số dư


# --- API ------------------------------------------------------------------------------------------


def _freeze(monkeypatch, at: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(paper, "datetime", _Frozen)


@needs_db
async def test_api_accounts_equity_includes_unrealized_at_latest_candle_and_today_pnl(client, db, monkeypatch):
    async with db() as s:
        await _win_cycle(s, T0)  # A, C chốt +1,70; D (60') vẫn mở từ 4300
        await _pred(s, T0 + 20 * M)
        await paper.open_for_new(s, T0 + 20 * M + timedelta(seconds=30))  # A, C mở lại ở 4300
        await _candle(s, T0 + 24 * M, 4301.0)
    _freeze(monkeypatch, T0 + 25 * M)
    assert (await client.post("/login", data={"key": KEY})).status_code == 303
    r = await client.get("/api/paper/accounts")
    assert r.status_code == 200
    d = r.json()
    acc = {a["code"]: a for a in d["accounts"]}
    assert acc["A"]["open"]["mark"] == 4301.0 and acc["A"]["open"]["unrealized"] == pytest.approx(0.7)
    assert acc["A"]["today"]["pnl"] == pytest.approx(1.7)
    assert acc["A"]["equity"] == pytest.approx(12.4)  # 10 + 1,70 chốt + 0,70 tạm
    assert acc["C"]["equity"] == pytest.approx(12.4)
    assert acc["D"]["equity"] == pytest.approx(10.7) and acc["D"]["today"]["pnl"] == 0
    assert acc["B"]["equity"] == 10.0 and acc["B"]["open"] is None
    assert d["today_pnl"] == pytest.approx(1.7 + 0.7 + 1.7 + 0.7 + 0.7)
    assert d["trade_date"] == "2026-09-23"


@needs_db
async def test_api_trades_before_pages_only_own_account_in_descending_id(client, db):
    async with db() as s:
        await _win_cycle(s, T0)
        await _pred(s, T0 + 20 * M)
        await paper.open_for_new(s, T0 + 20 * M + timedelta(seconds=30))
        await _pred(s, T0 + 22 * M)
        await paper.open_for_new(s, T0 + 22 * M + timedelta(seconds=30))
        ids = [t["id"] for t in await _trades(s, "A")]
        all_a = set((await s.execute(text("SELECT id FROM paper_trade WHERE account_code = 'A'"))).scalars())
    assert len(ids) == 3 and set(ids) == all_a
    assert (await client.post("/login", data={"key": KEY})).status_code == 303

    first = (await client.get(f"/api/paper/accounts/A/trades?before={ids[-1] + 1}")).json()["rows"]
    assert [r["id"] for r in first] == sorted(ids, reverse=True)
    page = (await client.get(f"/api/paper/accounts/A/trades?before={ids[-1]}")).json()["rows"]
    assert [r["id"] for r in page] == [ids[1], ids[0]]  # không lặp dòng đã thấy, không lẫn tài khoản khác
    tail = (await client.get(f"/api/paper/accounts/A/trades?before={ids[0]}")).json()["rows"]
    assert tail == []
    assert (await client.get("/api/paper/accounts/A/trades?before=0")).status_code == 422
    assert (await client.get("/api/paper/accounts/A/trades?before=-5")).status_code == 422


# --- gói dữ liệu cho não --------------------------------------------------------------------------


@needs_db
async def test_market_pack_has_paper_accounts_marked_fake_money(db):
    from goldpulse.services import context_pack

    async with db() as s:
        await _win_cycle(s, T0)
    async with db() as s:
        pack = await context_pack.market_pack(s, T0 + 20 * M)
    pa = pack["paper_accounts"]
    assert "tiền giả" in pa["note"]
    rows = {a["code"]: a for a in pa["accounts"]}
    assert set(rows) == {"A", "B", "C", "D"}
    assert rows["A"]["today_pnl_usd"] == pytest.approx(1.7) and rows["A"]["total_trades"] == 1
    assert rows["A"]["burned_days"] == "0/1"
    json.dumps(pack, ensure_ascii=False)  # không có date/Decimal lọt vào gói


# --- (v3) báo Telegram: chỉ đọc sổ ------------------------------------------------------


@needs_db
async def test_telegram_messages_open_close_no_backlog_no_repeat(db):
    from goldpulse.services import notify

    async with db() as s:
        await _win_cycle(s, T0 - 60 * M)  # lệnh cũ trước khi bật báo — không được gửi bù
        msgs, state = await notify.collect(s, None, T0)
        assert msgs == [] and state["last_trade_id"] > 0

        await _pred(s, T0)  # p_up 0,62 · vol96 1,5 → A, C vào; B bỏ (luật biến động); D còn lệnh cũ 60'
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        msgs, state = await notify.collect(s, state, T0 + M)
        assert len(msgs) == 1
        assert "Bot đoán TĂNG" in msgs[0] and "Giá vào 4300.00 · bot chắc 62%" in msgs[0]
        assert "Vào lệnh: A Gốc (đóng 21:15) · C Tự tin (đóng 21:15)" in msgs[0]
        assert "Bỏ qua: B Biến động — không khớp luật · D 1 giờ — đang có lệnh mở" in msgs[0]
        assert (await notify.collect(s, state, T0 + 2 * M))[0] == []  # không lặp

        await _candle(s, T0 + 14 * M, 4302.0)
        await paper.close_due(s, T0 + 17 * M)
        msgs, state = await notify.collect(s, state, T0 + 17 * M)
        # lệnh D cũ (mở trước khi bật báo) đóng sau khi bật → vẫn báo; không có nến trong 5' trước hạn → thị trường nghỉ
        assert [m.split(" đóng")[0] for m in msgs] == ["⏸ D 1 giờ", "✅ A Gốc", "✅ C Tự tin"]
        assert "Không tính vào đánh giá 4 tuần." in msgs[0]
        a = next(m for m in msgs if "A Gốc" in m)
        assert "+1.70 USD" in a and "TĂNG 4300.00 → 4302.00 · hết giờ giữ" in a
        assert "Hôm nay +3.40 (số dư 13.40) · cộng dồn +3.40" in a  # 2 lệnh thắng trong ngày
        assert (await notify.collect(s, state, T0 + 18 * M))[0] == []


def test_telegram_close_message_marks_burn_and_market_close():
    from goldpulse.services import notify

    base = {"account_code": "D", "account_name": "1 giờ", "direction": "down", "entry_price": 4200.0}
    burn = notify.close_message(
        {**base, "pnl": -10.0, "exit_price": 4210.0, "exit_reason": "stopout"},
        {"realized_pnl": -10.0, "end_equity": 0.0, "burned": True},
        -12.5,
    )
    assert burn.startswith("🔥 D 1 giờ đóng −10.00 USD") and "dừng tới 05:00" in burn and "cộng dồn −12.50" in burn
    mc = notify.close_message({**base, "pnl": 1.0, "exit_price": 4198.7, "exit_reason": "market_close"}, None, 0.0)
    assert mc.startswith("⏸") and "Không tính vào đánh giá 4 tuần." in mc
