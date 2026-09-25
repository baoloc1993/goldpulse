"""Luồng worker trên Postgres thật: A (HistData) · C (lấy bù) · metric độ phủ · smoke luồng lõi tới S1/S3.

HistData được thay bằng client giả trả nến dựng sẵn — không gọi mạng.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from goldpulse.integrations.dukascopy import RawCandle
from goldpulse.integrations.histdata import NY, FileNotAvailable, file_key
from goldpulse.services.calendar import open_minutes_of_day
from tests.conftest import needs_db

pytestmark = needs_db

FIX = Path(__file__).parent / "fixtures"
KEY = "test-key-0123456789"


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def tick(ts, price):
    return RawCandle(ts, price, price + 0.5, price - 0.5, price, 1.0)


class FakeHistData:
    """Giả HistDataClient: chia sẵn nến theo khoá file giống thật (năm đã qua → 'YYYY', năm nay → 'YYYY/M').
    `missing` = khoá chưa phát hành (FileNotAvailable)."""

    def __init__(self, candles, missing=(), all_missing=False):
        self.today = datetime.now(NY).date()
        self.files: dict[str, list[RawCandle]] = {}
        for c in sorted(candles, key=lambda c: c.ts):
            self.files.setdefault(file_key(c.ts.astimezone(NY).date(), self.today), []).append(c)
        self.missing = set(missing)
        self.all_missing = all_missing
        self.calls: list[str] = []

    async def fetch_file(self, key):
        self.calls.append(key)
        if self.all_missing or key in self.missing:
            raise FileNotAvailable(key)
        return list(self.files.get(key, []))

    async def close(self):
        pass


@pytest.fixture
async def db():
    from goldpulse.db import engine, session_scope
    from goldpulse.services import monitor

    async with session_scope() as s:
        await s.execute(
            text("TRUNCATE candle_1m, session_bar, crawl_day, feed_sample, query_log, worker_status RESTART IDENTITY")
        )
    monitor.invalidate_coverage_cache()
    yield session_scope
    monitor.invalidate_coverage_cache()
    await engine.dispose()


@pytest.fixture
async def worker(db):
    from goldpulse.worker.__main__ import Worker

    w = Worker()
    yield w
    await w.duka.close()
    await w.hd.close()
    await w.gold.close()


@pytest.fixture
async def client(db):
    from goldpulse.api.limiter import limiter
    from goldpulse.main import app

    limiter.reset()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/login", data={"key": KEY})
        assert r.status_code == 303
        yield c


async def add_pending(scope, *days):
    from goldpulse.repositories import crawl as crawl_repo

    async with scope() as s:
        await crawl_repo.insert_pending(s, [(d, len(open_minutes_of_day(d))) for d in days])


async def crawl_row(scope, day):
    async with scope() as s:
        return (
            (
                await s.execute(
                    text(
                        "SELECT status, attempts, candles, covered_minutes, traded_minutes, calendar_minutes, "
                        "last_error FROM crawl_day WHERE day = :d"
                    ),
                    {"d": day},
                )
            )
            .mappings()
            .one()
        )


def recent_weekday(days_ago: int) -> date:
    """Ngày UTC có phút thị trường mở, cách hôm nay ít nhất `days_ago` ngày."""
    d = datetime.now(UTC).date() - timedelta(days=days_ago)
    while not open_minutes_of_day(d) or len(open_minutes_of_day(d)) < 1380:
        d -= timedelta(days=1)
    return d


def histdata_from_fixture(day: date) -> list[RawCandle]:
    """Nến HistData dựng từ file Dukascopy thật: chỉ phút có tick (volume → 1.0), như CSV HistData."""
    from goldpulse.integrations.dukascopy import decode

    raw = decode(day, (FIX / f"XAUUSD_{day.isoformat()}.bi5").read_bytes())
    return [RawCandle(c.ts, c.open, c.high, c.low, c.close, 1.0) for c in raw if c.volume > 0]


# --- 1. Smoke luồng lõi: worker nạp ngày → session_bar → S3 → thẻ metric S1 --------------------------------


async def test_smoke_worker_loads_day_then_query_and_overview_show_it(db, worker, client):
    day = date(2024, 2, 15)
    candles = histdata_from_fixture(day)
    # giá đóng cửa hôm trước làm nền + dữ liệu sang ngày sau để biết ngày đã đủ
    candles = [tick(utc(2024, 2, 14, 21, 59), candles[0].open)] + candles + [tick(utc(2024, 2, 16, 1, 0), 2000.0)]
    worker.hd = FakeHistData(candles)
    await add_pending(db, day)

    from goldpulse.worker.__main__ import Outcome

    out = Outcome()
    await worker.history_histdata([day], out)
    assert out.stored == 1 and out.failed == 0

    row = await crawl_row(db, day)
    assert row["status"] == "ok" and row["covered_minutes"] == row["calendar_minutes"] == 1380

    # S3: phiên Âu 15/02/2024 có đúng biên độ = high − low của nến trong khung 07:00–16:00 UTC
    async with db() as s:
        hi, lo = (
            await s.execute(
                text("SELECT max(high), min(low) FROM candle_1m WHERE ts >= :a AND ts < :b"),
                {"a": utc(2024, 2, 15, 7), "b": utc(2024, 2, 15, 16)},
            )
        ).one()
    r = await client.get("/api/query", params={"session": "europe", "year_from": 2024, "year_to": 2024})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["avg_range"] == pytest.approx(round(hi - lo, 2), abs=0.01)

    # S1: thẻ độ phủ 100%, tiến độ đếm ngày ok, nguồn lịch sử báo ok (HistData)
    ov = (await client.get("/api/overview")).json()
    assert ov["metrics"]["coverage"]["value"] == 100.0
    assert ov["history"]["ok_days"] == 1 and ov["history"]["error_days"] == 0
    assert ov["history"]["source"] == "ok" and ov["history"]["provider"] == "histdata"


# --- 2. Luồng A với HistData ---------------------------------------------------------------------------


async def test_histdata_full_day_is_ok_with_flat_candles_volume_zero(db, worker):
    from goldpulse.worker.__main__ import Outcome

    day = date(2024, 2, 15)  # thứ Năm, 1380 phút mở
    start = utc(2024, 2, 15)
    ticks = [tick(start - timedelta(minutes=1), 2000.0), tick(start + timedelta(minutes=10), 2010.0)]
    ticks += [tick(utc(2024, 2, 15, 23, 59), 2020.0)]
    worker.hd = FakeHistData(ticks)
    await add_pending(db, day)

    await worker.history_histdata([day], Outcome())

    row = await crawl_row(db, day)
    assert row["status"] == "ok"
    assert row["covered_minutes"] == 1380 and row["traded_minutes"] == 2
    async with db() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT source, is_final, count(*), sum(volume) FROM candle_1m "
                    "WHERE ts >= :a AND ts < :b GROUP BY 1, 2"
                ),
                {"a": start, "b": start + timedelta(days=1)},
            )
        ).all()
        first = (await s.execute(text("SELECT close, volume FROM candle_1m WHERE ts = :t"), {"t": start})).one()
    assert rows == [("histdata", True, 1380, 2.0)]  # 1378 nến đứng giá volume 0 + 2 phút có tick
    assert first == (2000.0, 0.0)  # nến đứng giá đầu ngày lấy giá đóng cửa hôm trước


async def test_histdata_day_not_covered_to_edge_stays_pending_without_writing(db, worker):
    from goldpulse.worker.__main__ import Outcome

    day = date(2024, 2, 15)
    worker.hd = FakeHistData([tick(utc(2024, 2, 15, 0, 5), 2000.0), tick(utc(2024, 2, 15, 12, 0), 2001.0)])
    await add_pending(db, day)

    out = Outcome()
    await worker.history_histdata([day], out)

    row = await crawl_row(db, day)
    assert row["status"] == "pending" and row["attempts"] == 0  # không bị đốt lượt thử
    assert out.stored == 0 and out.failed == 0
    async with db() as s:
        n = (await s.execute(text("SELECT count(*) FROM candle_1m"))).scalar()
    assert n == 0  # không ghi nửa ngày rồi coi như xong


async def test_histdata_month_not_released_keeps_recent_day_pending(db, worker):
    from goldpulse.worker.__main__ import Outcome

    day = recent_weekday(10)
    worker.hd = FakeHistData([], all_missing=True)
    await add_pending(db, day)

    out = Outcome()
    await worker.history_histdata([day], out)

    row = await crawl_row(db, day)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert out.failed == 0


async def test_histdata_missing_file_for_old_day_is_error(db, worker):
    from goldpulse.worker.__main__ import Outcome

    day = date(2024, 2, 15)
    worker.hd = FakeHistData([], all_missing=True)
    await add_pending(db, day)

    out = Outcome()
    await worker.history_histdata([day], out)

    row = await crawl_row(db, day)
    assert row["status"] == "error" and row["attempts"] == 1
    assert row["last_error"]  # hiện ở danh sách ngày lỗi S2
    assert out.failed == 1


async def test_histdata_jan_2_uses_previous_year_close_for_opening_flat_candles(db, worker):
    """02/01/2024 (thứ Ba): phút đầu ngày UTC không có tick, giá nền nằm trong file năm 2023."""
    from goldpulse.worker.__main__ import Outcome

    day = date(2024, 1, 2)
    last_2023 = tick(utc(2023, 12, 29, 21, 58), 2062.5)  # NY thứ Sáu 29/12 16:58 → file "2023"
    first_2024 = tick(utc(2024, 1, 2, 1, 0), 2070.0)
    end_2024 = tick(utc(2024, 1, 2, 23, 59), 2065.0)
    worker.hd = FakeHistData([last_2023, first_2024, end_2024])
    assert set(worker.hd.files) == {"2023", "2024"}
    await add_pending(db, day)

    await worker.history_histdata([day], Outcome())

    assert "2023" in worker.hd.calls
    row = await crawl_row(db, day)
    assert row["status"] == "ok"
    assert row["covered_minutes"] == row["calendar_minutes"]  # không hở giờ đầu ngày
    async with db() as s:
        first = (
            await s.execute(text("SELECT close, volume FROM candle_1m WHERE ts = :t"), {"t": utc(2024, 1, 2)})
        ).one()
    assert first == (2062.5, 0.0)


# --- 3. Luồng C: lấy bù một lượt ----------------------------------------------------------------------


async def test_backfill_overwrites_temp_keeps_dukascopy_and_never_touches_crawl_day(db, worker, monkeypatch):
    from goldpulse.repositories import candles as candle_repo

    day = recent_weekday(5)
    minutes = open_minutes_of_day(day)
    m_duka, m_temp, m_edge = minutes[100], minutes[200], minutes[300]
    after_edge = minutes[400]

    async with db() as s:
        await candle_repo.upsert_final(s, [tick(m_duka, 1111.0)], source="dukascopy")
        await candle_repo.upsert_temp(s, m_temp, 1.0, 1.0, 1.0, 1.0)
    await add_pending(db, day)
    before = await crawl_row(db, day)

    ticks = [tick(minutes[0] - timedelta(minutes=1), 2000.0), tick(m_duka, 2222.0), tick(m_temp, 2333.0)]
    ticks.append(tick(m_edge, 2444.0))  # mép dữ liệu HistData (trễ ~1 tuần)
    worker.hd = FakeHistData(ticks)

    async def one_pass(*a, **k):
        worker.stop.set()

    monkeypatch.setattr(worker, "sleep", one_pass)
    await worker.backfill_loop()

    async with db() as s:
        got = {
            r[0]: (r[1], r[2], r[3])
            for r in await s.execute(
                text("SELECT ts, close, source, is_final FROM candle_1m WHERE ts IN (:a, :b, :c, :d)"),
                {"a": m_duka, "b": m_temp, "c": m_edge, "d": after_edge},
            )
        }
        n_crawl = (await s.execute(text("SELECT count(*) FROM crawl_day"))).scalar()
        status = await crawl_repo_status(s)
    assert got[m_duka] == (1111.0, "dukascopy", True)  # nến Dukascopy giữ nguyên
    assert got[m_temp] == (2333.0, "histdata", True)  # nến tạm gold-api bị đè bằng nến final
    assert got[m_edge] == (2444.0, "histdata", True)
    assert after_edge not in got  # không bịa nến đứng giá quá mép dữ liệu
    assert n_crawl == 1 and await crawl_row(db, day) == before  # KHÔNG ghi crawl_day
    assert str(day) in status["days"] and status["problems"] == []


async def crawl_repo_status(s):
    from goldpulse.repositories import feed as feed_repo

    return await feed_repo.get_status(s, "backfill")


# --- 4. Metric độ phủ ------------------------------------------------------------------------------------


async def test_coverage_counts_pending_and_error_as_missing_and_excludes_empty(db, client):
    from goldpulse.repositories import crawl as crawl_repo
    from goldpulse.services import monitor

    ok_day, pending_day, error_day, holiday = date(2024, 2, 12), date(2024, 2, 13), date(2024, 2, 14), date(2024, 2, 15)
    sat = date(2024, 2, 17)
    await add_pending(db, ok_day, pending_day, error_day, holiday, sat)
    async with db() as s:
        await crawl_repo.mark_done(s, ok_day, "ok", 1380, 1300, 1380, [])
        await crawl_repo.mark_error(s, error_day, "HTTP 404")
        await crawl_repo.mark_done(s, holiday, "empty", 1380, 0, 1380, [])  # lễ: loại khỏi mẫu số

        cov = await monitor.coverage(s, fresh=True)
    # 1380 phút có nến / (ok + pending + error) = 1380 / 4140; ngày lễ và thứ Bảy không vào mẫu số
    assert cov["total"]["covered_pct"] == 33.33
    y = cov["years"][0]
    assert (y["pending_days"], y["error_days"]) == (1, 1)
    assert {g["kind"] for g in y["gaps"]} == {"pending", "error"}
    assert [e["day"] for e in cov["errors"]] == ["2024-02-14"]

    monitor.invalidate_coverage_cache()
    ov = (await client.get("/api/overview")).json()
    assert ov["metrics"]["coverage"]["value"] == 33.33  # thẻ S1 không được báo 100% khi còn ngày chưa nạp


async def test_edge_days_awaiting_release_do_not_block_coverage_verdict(db, client):
    """Ngày mép HistData chưa phát hành không vào mẫu số và không làm S1 kẹt "đang nạp" mãi."""
    from goldpulse.repositories import crawl as crawl_repo
    from goldpulse.services import monitor

    done, edge1, edge2 = date(2024, 2, 12), date(2024, 2, 13), date(2024, 2, 14)
    await add_pending(db, done, edge1, edge2)
    async with db() as s:
        await crawl_repo.mark_done(s, done, "ok", 1380, 1300, 1380, [])
        prog = await crawl_repo.progress(s)
        cov = await monitor.coverage(s, fresh=True)
    assert prog["awaiting"] == 2 and prog["pending"] == 0
    assert cov["total"]["covered_pct"] == 100.0
    monitor.invalidate_coverage_cache()
    h = (await client.get("/api/overview")).json()["history"]
    assert h["done_days"] == h["total_days"] == 1 and h["awaiting_days"] == 2
