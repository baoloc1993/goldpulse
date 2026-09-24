import lzma
import struct
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from goldpulse.integrations.dukascopy import BadFile, day_url, decode
from goldpulse.services.ingest import build_temp_candle, summarize_day

FIX = Path(__file__).parent / "fixtures"


def test_url_month_is_zero_based():
    assert day_url(date(2024, 2, 15)).endswith("/XAUUSD/2024/01/15/BID_candles_min_1.bi5")
    assert day_url(date(2004, 12, 1)).endswith("/XAUUSD/2004/11/01/BID_candles_min_1.bi5")


def test_decode_real_file_2024():
    candles = decode(date(2024, 2, 15), (FIX / "XAUUSD_2024-02-15.bi5").read_bytes())
    assert len(candles) == 1440
    first = candles[0]
    assert first.ts == datetime(2024, 2, 15, tzinfo=UTC)
    assert first.open == pytest.approx(1993.685)  # giá vàng 15/02/2024 ~1990 USD
    assert first.low <= min(first.open, first.close) <= max(first.open, first.close) <= first.high
    assert candles[-1].ts == datetime(2024, 2, 15, 23, 59, tzinfo=UTC)


def test_decode_empty_and_corrupt():
    assert decode(date(2024, 1, 1), b"") == []
    with pytest.raises(BadFile):
        decode(date(2024, 1, 1), b"not lzma")
    with pytest.raises(BadFile):
        decode(date(2024, 1, 1), lzma.compress(b"x" * 25, format=lzma.FORMAT_ALONE))


def test_summarize_trading_day():
    d = date(2024, 2, 15)  # thứ Năm
    s = summarize_day(d, decode(d, (FIX / "XAUUSD_2024-02-15.bi5").read_bytes()))
    assert s.status == "ok"
    assert s.calendar == 1380
    assert s.covered == 1380
    assert s.missing == []
    assert 0 < s.traded <= s.covered


def test_summarize_2004_sparse_day():
    d = date(2004, 1, 5)
    s = summarize_day(d, decode(d, (FIX / "XAUUSD_2004-01-05.bi5").read_bytes()))
    assert s.status == "ok"
    assert s.covered == s.calendar  # Dukascopy điền nến đứng giá cho phút không có tick
    assert s.traded < s.covered  # 2004 thưa


def test_summarize_missing_minutes_listed_as_ranges():
    d = date(2024, 2, 15)
    candles = decode(d, (FIX / "XAUUSD_2024-02-15.bi5").read_bytes())
    kept = [c for c in candles if not (600 <= c.ts.hour * 60 + c.ts.minute < 606)]
    s = summarize_day(d, kept)
    assert s.covered == 1380 - 6
    assert s.missing == [["2024-02-15T10:00:00+00:00", "2024-02-15T10:05:00+00:00"]]


def test_saturday_file_is_empty_day():
    raw = b"".join(struct.pack(">5if", i * 60, 2000_000, 2000_000, 2000_000, 2000_000, 0.0) for i in range(1440))
    d = date(2024, 2, 17)
    s = summarize_day(d, decode(d, lzma.compress(raw, format=lzma.FORMAT_ALONE)))
    assert s.status == "empty"
    assert s.calendar == 0


def test_build_temp_candle():
    assert build_temp_candle([]) is None
    assert build_temp_candle([10.0, 12.0, 9.0, 11.0]) == (10.0, 12.0, 9.0, 11.0)
