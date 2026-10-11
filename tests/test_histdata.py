"""Nguồn HistData — không cần WARP/proxy."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from goldpulse.integrations.dukascopy import RawCandle
from goldpulse.integrations.histdata import file_key, parse_csv
from goldpulse.services.calendar import open_minutes_of_day
from goldpulse.services.ingest import day_complete, slice_and_fill, summarize_day
from tests.conftest import needs_db


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_parse_csv_uses_new_york_time_with_dst():
    """Đo thật: HistData ghi giờ New York CÓ đổi mùa hè (khớp Dukascopy), không phải EST cố định."""
    rows = parse_csv("20090910 101500;997.1;997.5;996.9;997.2;0\n20091120 101500;1142.0;1142.3;1141.8;1142.1;0\n")
    assert rows[0].ts == utc(2009, 9, 10, 14, 15)  # mùa hè: NY = UTC−4
    assert rows[1].ts == utc(2009, 11, 20, 15, 15)  # mùa đông: NY = UTC−5
    assert rows[0].close == 997.2 and rows[0].volume == 1.0


def test_file_key_year_vs_month():
    today = date(2026, 9, 25)
    assert file_key(date(2012, 5, 3), today) == "2012"
    assert file_key(date(2026, 8, 31), today) == "2026/8"
    assert file_key(date(2026, 9, 24), today) == "2026/9"


def _c(ts, price, vol=1.0):
    return RawCandle(ts, price, price, price, price, vol)


def test_slice_and_fill_flat_candles_for_minutes_without_ticks():
    day = date(2024, 2, 15)  # thứ Năm: 1380 phút mở
    start = utc(2024, 2, 15)
    candles = [_c(start - timedelta(minutes=5), 2000.0), _c(start + timedelta(minutes=3), 2001.0)]
    out = slice_and_fill(day, candles)
    assert len(out) == 1380
    assert out[0].close == 2000.0 and out[0].volume == 0.0  # nến đứng giá từ giá đóng cửa trước đó
    assert out[3].close == 2001.0 and out[3].volume == 1.0
    assert out[4].close == 2001.0 and out[4].volume == 0.0
    s = summarize_day(day, out)
    assert s.status == "ok" and s.covered == 1380 and s.traded == 1


def test_slice_and_fill_stops_at_data_edge_and_skips_without_base():
    day = date(2024, 2, 15)
    start = utc(2024, 2, 15)
    candles = [_c(start + timedelta(minutes=10), 2001.0)]
    out = slice_and_fill(day, candles, until=start + timedelta(minutes=20))
    assert out[0].ts == start + timedelta(minutes=10)  # không có giá trước đó → không bịa nến
    assert out[-1].ts == start + timedelta(minutes=20)


def test_day_complete():
    day = date(2024, 2, 15)
    last_open = open_minutes_of_day(day)[-1]
    assert day_complete(day, last_open)
    assert day_complete(day, last_open - timedelta(minutes=20))  # cuối phiên yên ắng vẫn tính là đủ
    assert not day_complete(day, last_open - timedelta(hours=3))
    assert not day_complete(day, None)
    assert day_complete(date(2024, 2, 17), None)  # thứ Bảy: không có phút mở


@needs_db
async def test_histdata_never_overwrites_dukascopy_but_overwrites_temp():
    from goldpulse.db import engine, session_scope
    from goldpulse.repositories import candles as candle_repo

    t1, t2 = utc(2009, 12, 1, 10, 0), utc(2009, 12, 1, 10, 1)
    async with session_scope() as s:
        await s.execute(text("TRUNCATE candle_1m"))
        await candle_repo.upsert_final(s, [_c(t1, 1200.0)], source="dukascopy")
        await candle_repo.upsert_temp(s, t2, 1.0, 1.0, 1.0, 1.0)
        await candle_repo.upsert_final(s, [_c(t1, 999.0), _c(t2, 1201.0)], source="histdata")
        rows = (await s.execute(text("SELECT ts, close, source, is_final FROM candle_1m ORDER BY ts"))).all()
    await engine.dispose()
    assert [(r[1], r[2], r[3]) for r in rows] == [(1200.0, "dukascopy", True), (1201.0, "histdata", True)]


def test_bad_csv_value_is_badfile_not_crash():
    import pytest

    from goldpulse.integrations.dukascopy import BadFile

    with pytest.raises(BadFile):
        parse_csv("20120102 180300;abc;1;1;1;0\n")


def test_ops_key_too_short_refuses_to_start():
    import pytest
    from pydantic import ValidationError

    from goldpulse.config import Settings

    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://x", ops_access_key="ngan")


@needs_db
async def test_histdata_flat_fill_never_replaces_real_temp_candle():
    """HistData thiếu tick (vd 3–7/2023) → nến đứng giá tự điền KHÔNG được đè nến tạm gold-api có giá thật."""
    from goldpulse.db import engine, session_scope
    from goldpulse.repositories import candles as candle_repo

    t_flat, t_tick = utc(2026, 9, 21, 10, 0), utc(2026, 9, 21, 10, 1)
    async with session_scope() as s:
        await s.execute(text("TRUNCATE candle_1m"))
        await candle_repo.upsert_temp(s, t_flat, 10.0, 12.0, 9.0, 11.0)
        await candle_repo.upsert_temp(s, t_tick, 10.0, 12.0, 9.0, 11.0)
        await candle_repo.upsert_final(s, [_c(t_flat, 5.0, vol=0.0), _c(t_tick, 20.0)], source="histdata")
        rows = (await s.execute(text("SELECT ts, high, source FROM candle_1m ORDER BY ts"))).all()
    await engine.dispose()
    assert [(r[1], r[2]) for r in rows] == [(12.0, "goldapi"), (20.0, "histdata")]


def test_cookie_secure_override_for_lan_http():
    from goldpulse.config import Settings

    base = {"database_url": "postgresql+asyncpg://x", "ops_access_key": "k" * 16, "app_env": "production"}
    assert Settings(**base).secure_cookie is True
    assert Settings(**base, cookie_secure=False).secure_cookie is False  # xem qua http trong LAN
