"""Nạp nến vào kho: tổng kết ngày, dựng nến tạm từ mẫu poll, tính lại session_bar."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations.dukascopy import RawCandle
from ..repositories import candles as candle_repo
from ..repositories import crawl as crawl_repo
from .calendar import (
    MINUTE,
    day_bounds,
    is_market_open,
    merge_ranges,
    open_minutes_of_day,
    session_window,
    sessions_touching,
)


@dataclass(frozen=True)
class DaySummary:
    status: str  # ok | empty
    candles: int
    traded: int  # phút trong lịch có giao dịch thật
    covered: int  # phút trong lịch có nến
    calendar: int
    missing: list[list[str]]  # [[từ, đến], ...] ISO UTC, đến = phút cuối (bao gồm)


def summarize_day(day: date, candles: Sequence[RawCandle]) -> DaySummary:
    open_minutes = open_minutes_of_day(day)
    by_ts = {c.ts: c for c in candles}
    covered = [m for m in open_minutes if m in by_ts]
    traded = sum(1 for m in covered if by_ts[m].volume > 0)
    # File không có phút nào có giao dịch thật → ngày lễ/cuối tuần, loại khỏi mẫu số
    any_traded = any(c.volume > 0 for c in candles)
    status = "ok" if any_traded else "empty"
    missing_minutes = [m for m in open_minutes if m not in by_ts] if status == "ok" else []
    missing = [[a.isoformat(), b.isoformat()] for a, b in merge_ranges(missing_minutes)]
    return DaySummary(status, len(candles), traded, len(covered), len(open_minutes), missing)


def build_temp_candle(prices: Sequence[float]) -> tuple[float, float, float, float] | None:
    """(open, high, low, close) từ các mẫu trong phút: đầu, cực đại, cực tiểu, cuối."""
    if not prices:
        return None
    return prices[0], max(prices), min(prices), prices[-1]


async def recompute_sessions(session: AsyncSession, start: datetime, end: datetime) -> None:
    for name, trade_date in sessions_touching(start, end):
        o, c = session_window(name, trade_date)
        await candle_repo.recompute_session(session, name, trade_date, o, c)


async def store_history_day(session: AsyncSession, day: date, candles: Sequence[RawCandle]) -> DaySummary:
    """Luồng A bước 4–5: upsert nến final, tính lại phiên chạm ngày, đánh dấu crawl_day."""
    summary = summarize_day(day, candles)
    await candle_repo.upsert_final(session, candles)
    start, end = day_bounds(day)
    await recompute_sessions(session, start, end)
    await crawl_repo.mark_done(
        session, day, summary.status, summary.candles, summary.traded, summary.covered, summary.missing
    )
    return summary


async def store_backfill_day(session: AsyncSession, day: date, candles: Sequence[RawCandle]) -> None:
    """Luồng C: nến final đè nến tạm + tính lại phiên. KHÔNG ghi crawl_day."""
    await candle_repo.upsert_final(session, candles)
    start, end = day_bounds(day)
    await recompute_sessions(session, start, end)


async def store_temp_minute(session: AsyncSession, minute: datetime, prices: Sequence[float]) -> bool:
    # gold-api vẫn trả giá "tươi" đứng yên lúc thị trường nghỉ — nến đó lọt vào chuỗi nến sống làm tín hiệu
    # bật giả sáng thứ Hai
    if not is_market_open(minute):
        return False
    ohlc = build_temp_candle(prices)
    if ohlc is None:
        return False
    await candle_repo.upsert_temp(session, minute, *ohlc)
    await recompute_sessions(session, minute, minute + MINUTE)
    return True


def slice_and_fill(day: date, candles: Sequence[RawCandle], until: datetime | None = None) -> list[RawCandle]:
    """Nến của ngày UTC `day` từ một dãy nến đã sắp xếp (HistData), điền nến đứng giá cho phút thị trường mở
    không có tick — cùng quy ước với Dukascopy. Không điền quá `until` (mép dữ liệu)."""
    start, end = day_bounds(day)
    by_ts = {c.ts: c for c in candles if start <= c.ts < end}
    prev = None
    for c in candles:  # giá đóng cửa cuối cùng trước ngày — nền cho nến đứng giá đầu ngày
        if c.ts >= start:
            break
        prev = c.close
    out: list[RawCandle] = []
    for m in open_minutes_of_day(day):
        if until is not None and m > until:
            break
        c = by_ts.get(m)
        if c is not None:
            out.append(c)
            prev = c.close
        elif prev is not None:
            out.append(RawCandle(m, prev, prev, prev, prev, 0.0))
    return out


def day_complete(day: date, last_ts: datetime | None) -> bool:
    """File dữ liệu đã phủ tới phút mở cuối cùng của ngày chưa (trừ hao 30 phút yên ắng cuối phiên)."""
    minutes = open_minutes_of_day(day)
    if not minutes:
        return True
    return last_ts is not None and last_ts >= minutes[-1] - 30 * MINUTE


async def store_histdata_day(session: AsyncSession, day: date, candles: Sequence[RawCandle]) -> DaySummary:
    """Như store_history_day nhưng không đè nến Dukascopy đã có (nguồn gốc 2004–2009)."""
    summary = summarize_day(day, candles)
    await candle_repo.upsert_final(session, candles, source="histdata")
    start, end = day_bounds(day)
    await recompute_sessions(session, start, end)
    await crawl_repo.mark_done(
        session, day, summary.status, summary.candles, summary.traded, summary.covered, summary.missing
    )
    return summary


async def store_histdata_backfill(session: AsyncSession, day: date, candles: Sequence[RawCandle]) -> None:
    await candle_repo.upsert_final(session, candles, source="histdata")
    start, end = day_bounds(day)
    await recompute_sessions(session, start, end)
