"""Lịch thị trường + khung phiên (ARCHITECTURE §6). Thuần tính toán, không I/O.

Lịch: mở Chủ nhật 18:00 → Thứ sáu 17:00 giờ New York, nghỉ hằng ngày 17:00–18:00 NY.
Phiên (giờ địa phương, zoneinfo tự đổi giờ mùa hè):
  asia   09:00–18:00 Asia/Tokyo
  europe 07:00–16:00 Europe/London
  us     08:00–17:00 America/New_York
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
VN = ZoneInfo("Asia/Ho_Chi_Minh")
MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class SessionDef:
    key: str
    label: str
    tz: ZoneInfo
    open: time
    close: time


SESSIONS: dict[str, SessionDef] = {
    "asia": SessionDef("asia", "Á", ZoneInfo("Asia/Tokyo"), time(9), time(18)),
    "europe": SessionDef("europe", "Âu", ZoneInfo("Europe/London"), time(7), time(16)),
    "us": SessionDef("us", "Mỹ", NY, time(8), time(17)),
}


def session_window(session: str, trade_date: date) -> tuple[datetime, datetime]:
    """Khung [mở, đóng) của phiên theo UTC; trade_date = ngày địa phương lúc phiên mở."""
    s = SESSIONS[session]
    start = datetime.combine(trade_date, s.open, tzinfo=s.tz).astimezone(UTC)
    end = datetime.combine(trade_date, s.close, tzinfo=s.tz).astimezone(UTC)
    return start, end


def sessions_touching(start: datetime, end: datetime) -> list[tuple[str, date]]:
    """Mọi khoá (phiên, trade_date) có khung giao với [start, end) UTC."""
    out: list[tuple[str, date]] = []
    d = (start - timedelta(days=1)).date()
    last = (end + timedelta(days=1)).date()
    while d <= last:
        for key in SESSIONS:
            o, c = session_window(key, d)
            if o < end and c > start:
                out.append((key, d))
        d += timedelta(days=1)
    return out


def active_sessions(now: datetime) -> list[tuple[str, date, datetime]]:
    """Phiên đang chạy lúc `now`: (phiên, trade_date, giờ đóng UTC)."""
    out = []
    for key, d in sessions_touching(now, now + MINUTE):
        o, c = session_window(key, d)
        if o <= now < c:
            out.append((key, d, c))
    return out


def session_hours_vn(on: date) -> dict[str, str]:
    """Giờ VN của từng phiên vào ngày `on` — tự đổi theo giờ mùa hè từng trung tâm."""
    out = {}
    for key in SESSIONS:
        o, c = session_window(key, on)
        out[key] = f"{o.astimezone(VN):%H:%M}–{c.astimezone(VN):%H:%M}"
    return out


def is_market_open(ts: datetime) -> bool:
    """Phút bắt đầu lúc ts có nằm trong lịch thị trường không."""
    local = ts.astimezone(NY)
    dow, hour = local.weekday(), local.hour  # Mon=0 … Sun=6
    if hour == 17:
        return False
    if dow == 5:  # thứ Bảy
        return False
    if dow == 4 and hour >= 17:  # thứ Sáu từ 17:00
        return False
    if dow == 6 and hour < 18:  # Chủ nhật trước 18:00
        return False
    return True


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


def open_minutes_of_day(day: date) -> list[datetime]:
    """Mọi phút thị trường mở trong ngày UTC `day` (tối đa 1440 phần tử).

    Mốc đóng/mở là đầu giờ NY và lệch múi NY–UTC là số giờ tròn → trạng thái không đổi
    trong một giờ UTC, chỉ cần xét 24 mốc thay vì 1440.
    """
    start, _ = day_bounds(day)
    out: list[datetime] = []
    for h in range(24):
        hour = start + timedelta(hours=h)
        if is_market_open(hour):
            out.extend(hour + i * MINUTE for i in range(60))
    return out


def merge_ranges(minutes: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Gộp các phút rời thành đoạn [từ, đến] (đến = phút cuối, bao gồm)."""
    out: list[tuple[datetime, datetime]] = []
    for m in sorted(minutes):
        if out and m - out[-1][1] == MINUTE:
            out[-1] = (out[-1][0], m)
        else:
            out.append((m, m))
    return out


def last_complete_day(now: datetime) -> date:
    """Ngày UTC cuối cùng đã kết thúc ≥ 24 giờ (ARCHITECTURE §7 A1)."""
    return (now - timedelta(days=2)).date()


# SQL tương đương is_market_open — dùng trong truy vấn metric độ trễ. Biến `t` là timestamptz.
MARKET_OPEN_SQL = """
    NOT (
      extract(hour FROM (t AT TIME ZONE 'America/New_York')) = 17
      OR extract(isodow FROM (t AT TIME ZONE 'America/New_York')) = 6
      OR (extract(isodow FROM (t AT TIME ZONE 'America/New_York')) = 5
          AND extract(hour FROM (t AT TIME ZONE 'America/New_York')) >= 17)
      OR (extract(isodow FROM (t AT TIME ZONE 'America/New_York')) = 7
          AND extract(hour FROM (t AT TIME ZONE 'America/New_York')) < 18)
    )
"""
