from datetime import UTC, date, datetime, timedelta

from goldpulse.services.calendar import (
    active_sessions,
    is_market_open,
    last_complete_day,
    merge_ranges,
    open_minutes_of_day,
    session_window,
    sessions_touching,
)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_session_windows_winter_and_summer():
    # Mùa đông: Á 00–09 UTC · Âu 07–16 UTC · Mỹ 13–22 UTC
    assert session_window("asia", date(2024, 1, 15)) == (utc(2024, 1, 15, 0), utc(2024, 1, 15, 9))
    assert session_window("europe", date(2024, 1, 15)) == (utc(2024, 1, 15, 7), utc(2024, 1, 15, 16))
    assert session_window("us", date(2024, 1, 15)) == (utc(2024, 1, 15, 13), utc(2024, 1, 15, 22))
    # Mùa hè: London và New York lùi một giờ UTC, Tokyo không đổi
    assert session_window("europe", date(2024, 7, 15)) == (utc(2024, 7, 15, 6), utc(2024, 7, 15, 15))
    assert session_window("us", date(2024, 7, 15)) == (utc(2024, 7, 15, 12), utc(2024, 7, 15, 21))
    assert session_window("asia", date(2024, 7, 15)) == (utc(2024, 7, 15, 0), utc(2024, 7, 15, 9))


def test_day_touches_only_its_own_three_sessions():
    """Challenge pha I: nạp ngày UTC D chỉ phải tính lại (phiên, D)."""
    for d in (date(2024, 1, 15), date(2024, 3, 12), date(2024, 7, 15), date(2024, 11, 4)):
        start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        keys = sessions_touching(start, start + timedelta(days=1))
        assert sorted(keys) == sorted([("asia", d), ("europe", d), ("us", d)])


def test_overlap_minute_belongs_to_two_sessions():
    names = {k for k, _, _ in active_sessions(utc(2024, 1, 15, 14, 30))}
    assert names == {"europe", "us"}


def test_market_calendar():
    assert not is_market_open(utc(2024, 2, 17, 12))  # thứ Bảy
    assert not is_market_open(utc(2024, 2, 18, 22, 30))  # CN 17:30 NY (mùa đông) — chưa mở
    assert is_market_open(utc(2024, 2, 18, 23, 0))  # CN 18:00 NY — mở
    assert not is_market_open(utc(2024, 2, 19, 22, 15))  # nghỉ hằng ngày 17:xx NY
    assert is_market_open(utc(2024, 2, 19, 23, 0))
    assert not is_market_open(utc(2024, 2, 16, 22, 0))  # thứ Sáu 17:00 NY — đóng tuần


def test_open_minutes_counts():
    assert len(open_minutes_of_day(date(2024, 2, 17))) == 0  # thứ Bảy
    assert len(open_minutes_of_day(date(2024, 2, 18))) == 60  # CN: 23:00–24:00 UTC
    assert len(open_minutes_of_day(date(2024, 2, 19))) == 1380  # thứ Hai: nghỉ 22:00–23:00 UTC
    assert len(open_minutes_of_day(date(2024, 2, 16))) == 1320  # thứ Sáu: đóng từ 22:00 UTC


def test_merge_ranges():
    m = timedelta(minutes=1)
    t = utc(2024, 1, 1)
    assert merge_ranges([t, t + m, t + 3 * m]) == [(t, t + m), (t + 3 * m, t + 3 * m)]


def test_last_complete_day_needs_24h_after_end():
    assert last_complete_day(utc(2024, 2, 20, 0, 0)) == date(2024, 2, 18)
    assert last_complete_day(utc(2024, 2, 20, 23, 59)) == date(2024, 2, 18)
