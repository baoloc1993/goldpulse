"""Luồng dữ liệu + phân quyền trên Postgres thật (DB goldpulse_test)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import needs_db

pytestmark = needs_db

FIX = Path(__file__).parent / "fixtures"
DAY = date(2024, 2, 15)


@pytest.fixture
async def db():
    from goldpulse.db import engine, session_scope

    async with session_scope() as s:
        await s.execute(
            text("TRUNCATE candle_1m, session_bar, crawl_day, feed_sample, query_log, worker_status RESTART IDENTITY")
        )
    from goldpulse.services import monitor

    monitor.invalidate_coverage_cache()
    yield session_scope
    await engine.dispose()


@pytest.fixture
async def client(db):
    from goldpulse.api.limiter import limiter
    from goldpulse.main import app

    limiter.reset()  # giới hạn 10 lần/phút ở /login đếm dồn qua các test
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as c:
        yield c


async def _login(client):
    r = await client.post("/login", data={"key": "test-key-0123456789"})
    assert r.status_code == 303
    return r


def _candles():
    from goldpulse.integrations.dukascopy import decode

    return decode(DAY, (FIX / "XAUUSD_2024-02-15.bi5").read_bytes())


async def _seed_day(scope):
    from goldpulse.repositories import crawl as crawl_repo
    from goldpulse.services import ingest
    from goldpulse.services.calendar import open_minutes_of_day

    async with scope() as s:
        await crawl_repo.insert_pending(s, [(DAY, len(open_minutes_of_day(DAY)))])
        return await ingest.store_history_day(s, DAY, _candles())


async def test_history_day_builds_sessions_and_coverage(db):
    summary = await _seed_day(db)
    assert summary.status == "ok"
    async with db() as s:
        rows = (await s.execute(text("SELECT session, trade_date, minutes_present, is_final FROM session_bar"))).all()
        assert {(r[0], r[1]) for r in rows} == {("asia", DAY), ("europe", DAY), ("us", DAY)}
        assert all(r[2] == 540 and r[3] for r in rows)  # 9 giờ × 60 phút, đều final
        from goldpulse.services import monitor

        cov = await monitor.coverage(s, fresh=True)
    assert cov["total"]["covered_pct"] == 100.0
    assert cov["years"][0]["year"] == 2024


async def test_temp_candle_never_overwrites_final(db):
    from goldpulse.services import ingest

    await _seed_day(db)
    minute = datetime(2024, 2, 15, 14, 0, tzinfo=UTC)
    async with db() as s:
        before = (await s.execute(text("SELECT close, source FROM candle_1m WHERE ts = :t"), {"t": minute})).one()
        await ingest.store_temp_minute(s, minute, [1.0, 2.0])
        after = (await s.execute(text("SELECT close, source FROM candle_1m WHERE ts = :t"), {"t": minute})).one()
    assert after == before and after[1] == "dukascopy"


async def test_final_candle_overwrites_temp(db):
    from goldpulse.services import ingest

    minute = datetime(2024, 2, 15, 14, 0, tzinfo=UTC)
    async with db() as s:
        await ingest.store_temp_minute(s, minute, [1.0, 3.0, 2.0])
        temp = (await s.execute(text("SELECT source, is_final FROM candle_1m WHERE ts = :t"), {"t": minute})).one()
        assert temp == ("goldapi", False)
        bar = (
            await s.execute(
                text("SELECT is_final FROM session_bar WHERE session='europe' AND trade_date=:d"), {"d": DAY}
            )
        ).one()
        assert bar[0] is False  # phiên có nến tạm → chưa final
    await _seed_day(db)
    async with db() as s:
        row = (await s.execute(text("SELECT source, is_final FROM candle_1m WHERE ts = :t"), {"t": minute})).one()
        bar = (
            await s.execute(
                text("SELECT is_final FROM session_bar WHERE session='europe' AND trade_date=:d"), {"d": DAY}
            )
        ).one()
    assert row == ("dukascopy", True)
    assert bar[0] is True


async def test_saturday_is_empty_without_download(db):
    from goldpulse.repositories import crawl as crawl_repo
    from goldpulse.services.calendar import open_minutes_of_day

    sat = date(2024, 2, 17)
    async with db() as s:
        await crawl_repo.insert_pending(s, [(sat, len(open_minutes_of_day(sat))), (DAY, 1380)])
        batch = await crawl_repo.next_batch(s, 10)
        status = (await s.execute(text("SELECT status FROM crawl_day WHERE day = :d"), {"d": sat})).scalar()
    assert status == "empty"
    assert batch == [DAY]


async def test_soft_fail_retries_then_errors(db):
    from goldpulse.repositories import crawl as crawl_repo

    async with db() as s:
        await crawl_repo.insert_pending(s, [(DAY, 1380)])
        for _ in range(4):
            await crawl_repo.mark_soft_fail(s, DAY, "HTTP 503")
        st = (await s.execute(text("SELECT status, attempts FROM crawl_day"))).one()
        assert st == ("pending", 4)
        assert await crawl_repo.next_batch(s, 10) == []  # đang nghỉ 2 phút
        await crawl_repo.mark_soft_fail(s, DAY, "HTTP 503")
        st = (await s.execute(text("SELECT status, attempts FROM crawl_day"))).one()
    assert st == ("error", 5)


async def test_access_matrix(client):
    """PERSONAS §2: không có khoá → mọi thứ bị chặn trừ /health (chỉ ok/down)."""
    h = await client.get("/health")
    assert h.status_code == 200 and h.json() == {"ok": True}
    for path in ("/overview", "/coverage", "/query"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == f"/login?next=%2F{path[1:]}"
    for path in ("/api/overview", "/api/coverage", "/api/query?session=us&year_from=2004&year_to=2026"):
        assert (await client.get(path)).status_code == 401
    assert (await client.post("/api/crawl-days/2024-02-15/retry")).status_code == 401
    assert (await client.post("/api/history/wake")).status_code == 401
    # Không có route sửa/xoá giá hay đặt lệnh
    for method, path in (("DELETE", "/api/candles"), ("PUT", "/api/candles"), ("POST", "/api/orders")):
        assert (await client.request(method, path)).status_code in (401, 404, 405)


async def test_login_wrong_key_and_cookie(client):
    r = await client.post("/login", data={"key": "sai"})
    assert r.status_code == 401 and "Khoá không đúng" in r.text
    assert "OPS_ACCESS_KEY" not in r.text  # không lộ tên biến môi trường cho người vận hành
    r = await _login(client)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "test-key-0123456789" not in cookie  # cookie không chứa khoá thô
    assert (await client.get("/api/overview")).status_code == 200


async def test_query_and_speed_metric(client, db):
    await _seed_day(db)
    await _login(client)
    r = await client.get("/api/query", params={"session": "europe", "year_from": 2004, "year_to": 2026})
    body = r.json()
    assert r.status_code == 200
    assert body["count"] == 1 and body["span_years"] == 23
    assert body["missing_years"] == [y for y in range(2004, 2027) if y != 2024]  # S3 phải nói rõ năm thiếu
    assert body["up_pct"] + body["down_pct"] + body["flat_pct"] == pytest.approx(100.0)
    r = await client.get("/api/query", params={"session": "europe", "year_from": 2024, "year_to": 2024, "dow": 5})
    assert r.json()["count"] == 0  # 15/02/2024 là thứ Năm
    assert (
        await client.get("/api/query", params={"session": "x", "year_from": 2004, "year_to": 2026})
    ).status_code == 422
    r = await client.get("/api/query", params={"session": "us", "year_from": 2026, "year_to": 2004})
    assert r.status_code == 422
    ov = (await client.get("/api/overview")).json()
    assert ov["metrics"]["speed"]["count"] == 1  # chỉ câu tra ≥ 20 năm vào metric


async def test_retry_error_day_idempotent(client, db):
    from goldpulse.repositories import crawl as crawl_repo

    async with db() as s:
        await crawl_repo.insert_pending(s, [(DAY, 1380)])
        for _ in range(5):
            await crawl_repo.mark_error(s, DAY, "Lỗi 404")
    await _login(client)
    r1 = await client.post("/api/crawl-days/2024-02-15/retry")
    r2 = await client.post("/api/crawl-days/2024-02-15/retry")
    assert r1.status_code == 200 and r2.status_code == 200
    async with db() as s:
        st = (await s.execute(text("SELECT status, attempts FROM crawl_day"))).one()
    assert st == ("pending", 0)
    assert (await client.post("/api/crawl-days/2024-02-16/retry")).status_code == 404
    assert (await client.post("/api/crawl-days/khong-phai-ngay/retry")).status_code == 422


async def test_latency_metric_counts_missing_minutes_as_fail(db):
    from goldpulse.repositories import feed as feed_repo

    # 10 phút gần nhất, chỉ 5 phút đầu có mẫu tươi — phút không có mẫu tính là trượt
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    since = now - timedelta(minutes=11)
    async with db() as s:
        for i in range(1, 6):
            t = since + timedelta(minutes=i, seconds=10)
            await feed_repo.insert_sample(s, t, t - timedelta(seconds=5), 2000.0, 5.0, True)
        open_min, good = await feed_repo.latency_metric(s, since)
    from goldpulse.services.calendar import is_market_open

    expected_open = sum(1 for i in range(1, 11) if is_market_open(since + timedelta(minutes=i)))
    assert open_min == expected_open
    assert good == sum(1 for i in range(1, 6) if is_market_open(since + timedelta(minutes=i)))


async def test_login_next_only_internal(client):
    """Sau khi nhập khoá quay về đúng màn cũ; không cho chuyển hướng ra ngoài (open redirect)."""
    r = await client.post("/login", data={"key": "test-key-0123456789", "next": "/query"})
    assert r.headers["location"] == "/query"
    r = await client.post("/login", data={"key": "test-key-0123456789", "next": "/query?session=us&year_from=2004"})
    assert r.headers["location"] == "/query?session=us&year_from=2004"  # giữ bộ lọc S3
    for bad in ("//evil.com", "https://evil.com", "/\evil.com", "/api/overview", "/login", "javascript:alert(1)"):
        r = await client.post("/login", data={"key": "test-key-0123456789", "next": bad})
        assert r.headers["location"] == "/overview"


async def test_html_404_page(client):
    r = await client.get("/khong-ton-tai")
    assert r.status_code == 404 and "Không có trang này" in r.text
    assert (await client.get("/api/khong-ton-tai")).headers["content-type"].startswith("application/json")


async def test_login_rate_limited(client):
    codes = [(await client.post("/login", data={"key": "sai"})).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[-1] == 429


def test_client_ip_trusts_cloudflare_header_only_from_internal_peer():
    """Sau Tunnel peer là 127.0.0.1 hoặc IP bridge Docker → dùng CF-Connecting-IP; peer ngoài thì không tin."""
    from starlette.requests import Request

    from goldpulse.api.limiter import client_ip
    from goldpulse.config import settings

    def req(peer, cf=None):
        headers = [(b"cf-connecting-ip", cf.encode())] if cf else []
        return Request({"type": "http", "client": (peer, 1234), "headers": headers})

    settings.trust_cloudflare_ip = False  # mặc định: không Tunnel → header tự đặt không được tin
    assert client_ip(req("172.18.0.1", "203.0.113.7")) == "172.18.0.1"
    settings.trust_cloudflare_ip = True
    assert client_ip(req("127.0.0.1", "203.0.113.7")) == "203.0.113.7"
    assert client_ip(req("172.18.0.1", "203.0.113.7")) == "203.0.113.7"  # Docker bridge
    assert client_ip(req("8.8.8.8", "203.0.113.7")) == "8.8.8.8"  # người ngoài tự đặt header
    assert client_ip(req("127.0.0.1")) == "127.0.0.1"
    settings.trust_cloudflare_ip = False


async def test_non_ascii_cookie_is_rejected_not_500(client):
    r = await client.get("/overview", headers=[(b"cookie", "gp_ops=khóa".encode())])
    assert r.status_code == 303


async def test_login_rate_limit_message_is_vietnamese(client):
    for _ in range(10):
        await client.post("/login", data={"key": "sai"})
    r = await client.post("/login", data={"key": "sai"})
    assert r.status_code == 429 and "đợi 1 phút" in r.text


async def test_security_headers(client):
    r = await client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert "<script>" not in r.text  # CSP chặn script inline — mọi script ở /static
