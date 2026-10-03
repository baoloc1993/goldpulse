"""(v4) BTC độc lập — luồng tiền: mở theo tín hiệu, hết hạn, cháy, hết ngày, bỏ qua; phân quyền + không lẫn với vàng.

Tiền giả. Mọi kịch bản truyền `now` tường minh; giờ UTC. Tài khoản 10 USD, x20 → lệnh 200 USD, phí + trượt 0,14%.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from goldpulse.repositories import inst as repo
from goldpulse.services.btc import SYMBOL, paper
from goldpulse.services.btc import notify as btc_notify
from tests.conftest import needs_db

KEY = "test-key-0123456789"
M = timedelta(minutes=1)
H = timedelta(hours=1)
DAY = date(2026, 10, 1)
BAR = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)  # nến 1h 10:00 → vào ở close nến 1m 10:59, mở lúc 11:00
P0 = 100_000.0


@pytest.fixture
async def db():
    from goldpulse.db import engine, session_scope

    async with session_scope() as s:
        await s.execute(
            text(
                "TRUNCATE inst_trade, inst_day, inst_signal, inst_candle, inst_feed, worker_status"
                " RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(text("UPDATE inst_account SET enabled = true"))
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


async def _candles(s, start: datetime, prices: list[float], low_dip: dict[int, float] | None = None) -> None:
    """Một nến 1m mỗi phút từ `start`; open = close = giá; `low_dip`/high theo chỉ số để giả cú giật."""
    low_dip = low_dip or {}
    rows = []
    for i, p in enumerate(prices):
        lo = low_dip.get(i, p)
        rows.append((start + i * M, p, max(p, lo), min(p, lo), p, 1.0))
    await repo.upsert_candles(s, SYMBOL, rows)


def _sig(bar: datetime, **fired) -> dict:
    base = {"model": 0, "model_trend": 0, "spike": 0, "squeeze": 0}
    base.update(fired)
    return {"bar_ts": bar, "close": P0, "values": {"p_up": 0.7, "conf": 0.2, "ema_gap": 0.01}, "fired": base}


async def _rows(s, code: str) -> list[dict]:
    return list(reversed(await repo.trades_page(s, code, None, 100)))


@needs_db
async def test_seed_bon_tai_khoan_btc(db):
    async with db() as s:
        accs = await repo.accounts(s, SYMBOL)
    assert [a["code"] for a in accs] == ["BTC-A", "BTC-B", "BTC-C", "BTC-D"]
    assert {a["strategy"]["kind"] for a in accs} == {"model", "model_trend", "spike", "squeeze"}
    assert all(a["leverage"] == 20 and a["start_equity"] == 10 for a in accs)


@needs_db
async def test_mo_va_dong_het_han_dung_cong_thuc(db):
    async with db() as s:
        await _candles(s, BAR + 59 * M, [P0])  # nến vào 10:59
        opened, _ = await paper.open_from_signal(s, _sig(BAR, model=1), P0, BAR + H + 70 * timedelta(seconds=1))
        assert opened == 1
        await _candles(s, BAR + H, [P0 * 1.001] * 59 + [P0 * 1.005])  # 11:00..11:59, đóng ở 11:59 = +0,5%
        assert await paper.close_open(s, BAR + 2 * H + M) == 1
        t = (await _rows(s, "BTC-A"))[0]
        assert t["status"] == "closed" and t["exit_reason"] == "horizon"
        assert t["notional"] == pytest.approx(200.0)
        assert t["pnl"] == pytest.approx(200 * (0.005 - 0.0014), abs=1e-4)
        d = await repo.day_get(s, "BTC-A", DAY)
        assert d["n_trades"] == 1 and not d["burned"]


@needs_db
async def test_chay_khi_gia_di_nguoc_5_phan_tram(db):
    async with db() as s:
        await _candles(s, BAR + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(BAR, model=1), P0, BAR + H + M)
        # phút thứ 10 low −5%: (10 − 200 × 0,0014) / 200 = 4,86% < 5% → cháy
        await _candles(s, BAR + H, [P0] * 30, low_dip={10: P0 * 0.95})
        await paper.close_open(s, BAR + H + 30 * M)
        t = (await _rows(s, "BTC-A"))[0]
        assert t["exit_reason"] == "stopout" and t["pnl"] == pytest.approx(-10.0)
        assert (await repo.day_get(s, "BTC-A", DAY))["burned"]
        # Giờ sau có tín hiệu → bỏ qua vì đã cháy
        await _candles(s, BAR + H + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(BAR + H, model=-1), P0, BAR + 2 * H + M)
        assert (await _rows(s, "BTC-A"))[-1]["skip_reason"] == "burned"


@needs_db
async def test_het_ngay_dong_o_nen_2359_khong_tinh_danh_gia(db):
    bar = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)  # D giữ 4h: mở 22:00, hạn 02:00 hôm sau → đóng 23:59
    async with db() as s:
        await _candles(s, bar + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(bar, squeeze=-1), P0, bar + H + M)
        await _candles(s, bar + H, [P0 * 0.999] * 120)  # 22:00..23:59
        await paper.close_open(s, bar + 3 * H + M)
        t = (await _rows(s, "BTC-D"))[0]
        assert t["exit_reason"] == "day_close" and t["closed_at"] == datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
        assert t["pnl"] == pytest.approx(200 * (0.001 - 0.0014), abs=1e-4)
        tot = await repo.totals(s, "BTC-D")
        assert tot["trades"] == 1 and tot["eval_n"] == 0  # PRD §6: không tính vào số lệnh đánh giá


@needs_db
async def test_nen_2300_khong_mo_va_bo_qua_dung_ly_do(db):
    async with db() as s:
        late = datetime(2026, 10, 1, 23, 0, tzinfo=UTC)
        await _candles(s, late + 59 * M, [P0])
        assert await paper.open_from_signal(s, _sig(late, model=1), P0, late + H + M) == (0, 0)
        assert await _rows(s, "BTC-A") == []
        # muộn > 5 phút → stale; đang có lệnh → open_trade; tắt → disabled
        await _candles(s, BAR + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(BAR, model=1, spike=1), P0, BAR + H + 6 * M)
        assert [r["skip_reason"] for r in await _rows(s, "BTC-A")] == ["stale"]
        bar2 = BAR + H
        await _candles(s, bar2 + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(bar2, spike=1), P0, bar2 + H + M)
        bar3 = BAR + 2 * H
        await _candles(s, bar3 + 59 * M, [P0])
        await repo.set_enabled(s, "BTC-B", False)
        await paper.open_from_signal(s, _sig(bar3, spike=-1, model_trend=1), P0, bar3 + H + M)
        assert (await _rows(s, "BTC-C"))[-1]["skip_reason"] == "open_trade"
        assert (await _rows(s, "BTC-B"))[-1]["skip_reason"] == "disabled"


@needs_db
async def test_lenh_het_han_dung_gio_nhuong_cho_tin_hieu_moi(db):
    """Bản nghiên cứu cho vào lại ngay nến lệnh cũ đóng (`e ≥ busy`) — process_hours đóng TRƯỚC rồi mới mở."""
    async with db() as s:
        await _candles(s, BAR + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(BAR, model=1), P0, BAR + H + M)
        await _candles(s, BAR + H, [P0] * 60)  # hạn 12:00 — nến 11:59 là nến đóng lệnh VÀ nến vào của giờ 11:00
        await paper.close_open(s, BAR + 2 * H + M)
        await paper.open_from_signal(s, _sig(BAR + H, model=-1), P0, BAR + 2 * H + M)
        rows = await _rows(s, "BTC-A")
        assert [r["status"] for r in rows] == ["closed", "open"]


@needs_db
async def test_telegram_btc_rieng_khong_lan_voi_vang(db):
    async with db() as s:
        msgs, state = await btc_notify.collect(s, None, BAR)
        assert msgs == [] and state["last_trade_id"] == 0  # lần đầu: đặt mốc, không gửi bù
        await _candles(s, BAR + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(BAR, model=1), P0, BAR + H + M)
        msgs, state = await btc_notify.collect(s, state, BAR + H + M)
        assert len(msgs) == 1 and "BTC" in msgs[0] and "A Mô hình" in msgs[0] and "Tiền giả" in msgs[0]
        from goldpulse.services import notify as gold_notify

        gmsgs, _ = await gold_notify.collect(s, {"last_trade_id": 0, "closed_ids": []}, BAR + H + M)
        assert gmsgs == []  # bot vàng không thấy lệnh BTC


@needs_db
async def test_api_btc_can_khoa_va_khong_lan_voi_vang(client):
    for path in ("/api/btc/accounts", "/api/btc/accounts/BTC-A", "/api/btc/accounts/BTC-A/trades?before=5"):
        assert (await client.get(path)).status_code == 401, path
    assert (await client.post("/api/btc/accounts/BTC-A/enabled", json={"enabled": False})).status_code == 401
    r = await client.get("/btc")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert (await client.post("/login", data={"key": KEY, "next": "/btc/BTC-C"})).headers["location"] == "/btc/BTC-C"
    d = (await client.get("/api/btc/accounts")).json()
    assert [a["code"] for a in d["accounts"]] == ["BTC-A", "BTC-B", "BTC-C", "BTC-D"]
    assert d["thresholds"] == {"go_min_trades": 30, "go_max_burn_ratio": 0.2}
    gold = (await client.get("/api/paper/accounts")).json()
    assert all(not a["code"].startswith("BTC") for a in gold["accounts"])
    # mã vàng / mã lạ / body lạ — không đổi được gì
    assert (await client.post("/api/btc/accounts/A/enabled", json={"enabled": False})).status_code == 422
    assert (await client.post("/api/btc/accounts/BTC-Z/enabled", json={"enabled": False})).status_code == 404
    assert (await client.post("/api/btc/accounts/BTC-A/enabled", json={"enabled": "no"})).status_code == 422
    assert (await client.post("/api/paper/accounts/BTC-A/enabled", json={"enabled": False})).status_code in (404, 422)
    assert (await client.delete("/api/btc/accounts/BTC-A")).status_code == 405
    assert (await client.post("/api/btc/accounts/BTC-A/trades", json={})).status_code == 405
    r = await client.post("/api/btc/accounts/BTC-D/enabled", json={"enabled": False})
    assert r.status_code == 200
    assert not next(a for a in (await client.get("/api/btc/accounts")).json()["accounts"] if a["code"] == "BTC-D")[
        "enabled"
    ]
    assert (await client.get("/btc/BTC-Z")).status_code == 404
    assert (await client.get("/btc/btc-a")).status_code == 404
    assert (await client.get("/btc")).status_code == 200
