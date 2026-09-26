"""(v3) Tài khoản giấy: luật vào lệnh · khớp theo dự đoán tự động · đóng đúng hạn · cháy · phân quyền. Tiền giả.

Giờ cố định trong tuần (thứ Tư 23/09/2026 10:00 New York) để lịch thị trường không làm test chập chờn.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from goldpulse.repositories import paper as repo
from goldpulse.repositories import predictions as pred_repo
from goldpulse.services import paper
from tests.conftest import needs_db

KEY = "test-key-0123456789"
T0 = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)  # 10:00 NY thứ Tư — thị trường mở


# --- tính toán thuần -----------------------------------------------------------------------------


def test_trade_date_rolls_at_17_new_york():
    assert paper.trade_date_of(datetime(2026, 9, 23, 20, 59, tzinfo=UTC)) == date(2026, 9, 23)  # 16:59 NY
    assert paper.trade_date_of(datetime(2026, 9, 23, 22, 0, tzinfo=UTC)) == date(2026, 9, 24)  # 18:00 NY → ngày kế


def test_rules_and_pnl():
    sig = {"vol96": 2.4, "model": {"p_up": 0.58}}
    assert paper.rule_allows({"kind": "all"}, {})
    assert paper.rule_allows({"kind": "volatility", "min_vol": 2.0}, sig)
    assert not paper.rule_allows({"kind": "volatility", "min_vol": 2.0}, {"vol96": 1.9})
    assert not paper.rule_allows({"kind": "volatility", "min_vol": 2.0}, {})  # dự đoán cũ thiếu vol96 → không đạt
    assert not paper.rule_allows({"kind": "confidence", "min_conf": 0.0852}, sig)  # |0,58 − 0,5| = 0,08
    assert paper.rule_allows({"kind": "confidence", "min_conf": 0.0852}, {"model": {"p_up": 0.40}})
    acc = {"oz": 1.0, "spread": 0.3}
    assert paper.pnl_of(acc, "up", 4300.0, 4302.0) == pytest.approx(1.7)
    assert paper.pnl_of(acc, "down", 4300.0, 4302.0) == pytest.approx(-2.3)


def test_verdict_thresholds():
    base = {"days": 20, "burned_days": 2, "eval_n": 45, "eval_pnl": 3.0}
    assert paper.verdict(base) == "go"
    assert paper.verdict({**base, "eval_n": 20}) == "pivot"
    assert paper.verdict({**base, "burned_days": 5}) == "pivot"  # 25% ngày cháy > 20%
    assert paper.verdict({**base, "eval_pnl": -0.1}) == "none"


# --- trên Postgres thật ---------------------------------------------------------------------------


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


async def _candle(s, ts: datetime, close: float) -> None:
    await s.execute(
        text("""INSERT INTO candle_1m (ts, open, high, low, close, volume, source, is_final, updated_at)
                VALUES (:t, :c, :c, :c, :c, 1, 'histdata', true, now())
                ON CONFLICT (ts) DO UPDATE SET close = EXCLUDED.close"""),
        {"t": ts, "c": close},
    )


async def _pred(s, made: datetime, direction="up", p_up=0.62, vol96=1.5, backend="cli", source="auto", price=4300.0):
    return await pred_repo.insert(
        s,
        {
            "made_at": made,
            "source": source,
            "direction": direction,
            "confidence": "low",
            "price_at": price,
            "horizon_minutes": 15,
            "due_at": made + timedelta(minutes=15),
            "signals": {"vol96": vol96, "model": {"p_up": p_up}},
            "reasoning": "",
            "backend": backend,
            "model": "opus",
            "job_id": None,
        },
    )


async def _by_account(s) -> dict[str, dict]:
    rows = (await s.execute(text("SELECT * FROM paper_trade ORDER BY id"))).mappings()
    return {r["account_code"]: dict(r) for r in rows}


@needs_db
async def test_prediction_opens_per_rule_and_ignores_fake_and_chat(db):
    async with db() as s:
        await _pred(s, T0 - timedelta(minutes=1), backend="fake")
        await _pred(s, T0 - timedelta(minutes=1), source="chat")
        pid = await _pred(s, T0, p_up=0.62, vol96=1.5)
        assert await paper.open_for_new(s, T0 + timedelta(seconds=30)) == 3
        # nhịp sau (hoặc worker khởi động lại) không mở trùng
        assert await paper.open_for_new(s, T0 + timedelta(seconds=90)) == 0
    async with db() as s:
        t = await _by_account(s)
    assert set(t) == {"A", "B", "C", "D"} and all(x["prediction_id"] == pid for x in t.values())
    assert t["A"]["status"] == "open" and t["C"]["status"] == "open" and t["D"]["status"] == "open"
    assert t["B"]["status"] == "skipped" and t["B"]["skip_reason"] == "rule"  # biến động 1,5 < 2,0
    assert t["A"]["entry_price"] == 4300.0 and t["A"]["due_at"] == T0 + timedelta(minutes=15)
    assert t["D"]["due_at"] == T0 + timedelta(minutes=60)


@needs_db
async def test_one_open_trade_per_account_and_close_at_horizon(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        await _pred(s, T0 + timedelta(minutes=5), direction="down")
        await paper.open_for_new(s, T0 + timedelta(minutes=5, seconds=30))
        second = (await repo.trades_page(s, "A", None, 5))[0]
        assert second["status"] == "skipped" and second["skip_reason"] == "open_trade"
        # nến phút cuối trước hạn 14:15 là 14:14 → giá ra
        await _candle(s, T0 + timedelta(minutes=14), 4302.0)
        closed = await paper.close_due(s, T0 + timedelta(minutes=17))
        assert closed >= 1
    async with db() as s:
        a = (await repo.trades_page(s, "A", None, 5))[-1]
        day = await repo.day_get(s, "A", date(2026, 9, 23))
        d_open = await repo.open_trade_of(s, "D")
    assert a["status"] == "closed" and a["exit_reason"] == "horizon" and a["exit_price"] == 4302.0
    assert a["pnl"] == pytest.approx(1.7)  # (4302 − 4300) − 0,30
    assert day["n_trades"] == 1 and day["n_wins"] == 1 and day["end_equity"] == pytest.approx(11.7)
    assert d_open is not None  # luật D giữ 60 phút — chưa tới hạn


@needs_db
async def test_burn_closes_early_and_blocks_rest_of_day(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        # giá rơi 10 USD lúc 14:03, lệnh mua 1 oz → 10 − 10 − 0,3 < 0 → cháy
        await _candle(s, T0 + timedelta(minutes=3), 4290.0)
        await paper.close_due(s, T0 + timedelta(minutes=4))
        a = (await repo.trades_page(s, "A", None, 1))[0]
        assert a["status"] == "closed" and a["exit_reason"] == "stopout" and a["pnl"] == pytest.approx(-10.3)
        day = await repo.day_get(s, "A", date(2026, 9, 23))
        assert day["burned"] and day["end_equity"] == 0
        await _pred(s, T0 + timedelta(minutes=30))
        await paper.open_for_new(s, T0 + timedelta(minutes=30, seconds=20))
        nxt = (await repo.trades_page(s, "A", None, 1))[0]
    assert nxt["status"] == "skipped" and nxt["skip_reason"] == "burned"


@needs_db
async def test_stale_price_does_not_burn_and_stale_prediction_is_skipped(db):
    async with db() as s:
        await _pred(s, T0)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
        # nến lỗ nặng nhưng đã cũ 10 phút → nguồn đứng, không được cháy ảo
        await _candle(s, T0 + timedelta(minutes=1), 4280.0)
        await paper.close_due(s, T0 + timedelta(minutes=11))
        assert (await repo.open_trade_of(s, "A")) is not None
        # worker vắng: dự đoán 10 phút tuổi mới được xử → không vào giá cũ
        await _pred(s, T0 + timedelta(minutes=20))
        await paper.open_for_new(s, T0 + timedelta(minutes=30))
        c = (await repo.trades_page(s, "C", None, 1))[0]
    assert c["skip_reason"] in ("open_trade", "stale")
    async with db() as s:
        b = (await repo.trades_page(s, "B", None, 1))[0]
    assert b["skip_reason"] == "rule"  # luật kiểm TRƯỚC điều kiện tài khoản


@needs_db
async def test_no_open_near_daily_break(db):
    near = datetime(2026, 9, 23, 20, 50, tzinfo=UTC)  # 16:50 NY — 10 phút trước giờ nghỉ
    async with db() as s:
        await _pred(s, near)
        await paper.open_for_new(s, near + timedelta(seconds=20))
        t = await _by_account(s)
    assert t["A"]["skip_reason"] == "market_closing" and t["D"]["skip_reason"] == "market_closing"


@needs_db
async def test_every_v3_route_requires_key_and_only_enabled_is_writable(client, db):
    for path in ("/api/paper/accounts", "/api/paper/accounts/A", "/api/paper/accounts/A/trades?before=1"):
        assert (await client.get(path)).status_code == 401, path
    assert (await client.post("/api/paper/accounts/A/enabled", json={"enabled": False})).status_code == 401
    for page in ("/paper", "/paper/A"):
        r = await client.get(page)
        assert r.status_code == 303 and "/login" in r.headers["location"], page

    assert (await client.post("/login", data={"key": KEY})).status_code == 303
    d = (await client.get("/api/paper/accounts")).json()
    assert [a["code"] for a in d["accounts"]] == ["A", "B", "C", "D"]
    assert all(a["equity"] == 10.0 and a["open"] is None for a in d["accounts"])
    assert (await client.get("/paper")).status_code == 200 and (await client.get("/paper/B")).status_code == 200
    assert (await client.get("/paper/Z")).status_code == 404
    assert (await client.post("/api/paper/accounts/Z/enabled", json={"enabled": False})).status_code == 404
    assert (await client.post("/api/paper/accounts/A/enabled", json={"enabled": "yes"})).status_code == 422
    assert (await client.post("/api/paper/accounts/a1/enabled", json={"enabled": False})).status_code == 422
    # không có đường ghi lệnh / đổi vốn qua web
    assert (await client.post("/api/paper/accounts/A/trades", json={})).status_code == 405
    assert (await client.delete("/api/paper/accounts/A")).status_code == 405
    r = await client.post("/api/paper/accounts/C/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"code": "C", "enabled": False}

    async with db() as s:
        await _pred(s, T0, p_up=0.40)
        await paper.open_for_new(s, T0 + timedelta(seconds=30))
    one = (await client.get("/api/paper/accounts/C")).json()
    assert one["enabled"] is False and one["rows"][0]["skip_reason"] == "disabled"


@needs_db
async def test_login_returns_to_s9_and_bad_code_page_is_html_404(client, db):
    from goldpulse.api.routes.pages import safe_next

    assert safe_next("/paper/A") == "/paper/A" and safe_next("/paper") == "/paper"
    assert safe_next("/paper/AB") == "/overview" and safe_next("//evil.com/paper/A") == "/overview"
    r = await client.post("/login", data={"key": KEY, "next": "/paper/A"})
    assert r.status_code == 303 and r.headers["location"] == "/paper/A"
    bad = await client.get("/paper/a")
    assert bad.status_code == 404 and "text/html" in bad.headers["content-type"]
