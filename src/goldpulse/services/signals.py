"""Tín hiệu 15 phút + ngữ cảnh cho não.

Ba tín hiệu trên nến 15 phút gộp từ phút sống, 60 nến đã đóng gần nhất:
  bollinger  %b(20, 2σ) < 0 → lên · > 100 → xuống
  streak     ≥ 6 nến liên tiếp close giảm (so close nến trước) → lên · ≥ 6 tăng → xuống
  rsi9       RSI 9 (Wilder) < 20 → lên · > 80 → xuống
Ngữ cảnh (KHÔNG có sức đoán 15 phút theo backtest — chỉ để não mô tả): vị trí trong biên 5/20/60 ngày,
RSI 14 khung 1h/4h/1D chỉ trên nến đã đóng.
"""

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import candles as candle_repo
from .calendar import VN, is_market_open

BAR_MINUTES = 15
N_BARS = 60
STREAK_MIN = 6
# Dự đoán TỰ ĐỘNG chỉ khi ≥ 2 tín hiệu cùng bật (chủ dự án chốt 2026-09-26, phương án 2): kiểm toàn kho 2012–2026 —
# ≥ 2 tín hiệu 56–57% (2012–21), ~54% (2022–26) và ổn định nhất; mọi tín hiệu chỉ còn 52,2% (2025–26). Chat không đổi.
AUTO_MIN_FIRED = 2
LOOKBACK = timedelta(days=10)  # 60 nến 15m sống nằm gọn trong vài ngày giao dịch, kể cả qua cuối tuần/lễ


def bollinger_pct_b(closes: list[float], period: int = 20, k: float = 2.0) -> float | None:
    if len(closes) < period:
        return None
    win = closes[-period:]
    mean = sum(win) / period
    sd = math.sqrt(sum((x - mean) ** 2 for x in win) / period)
    if sd == 0:
        return None
    lower = mean - k * sd
    return (closes[-1] - lower) / (2 * k * sd) * 100


def streak(closes: list[float]) -> int:
    """Số nến liên tiếp cùng hướng close (so với close nến trước), có dấu: +n tăng, −n giảm; đứng giá cắt chuỗi."""
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        d = closes[i] - closes[i - 1]
        s = 1 if d > 0 else -1 if d < 0 else 0
        if s == 0 or (n and (s > 0) != (n > 0)):
            break
        n += s
    return n


def rsi(closes: list[float], period: int) -> float | None:
    """RSI Wilder (EMA α = 1/period), khởi động bằng trung bình `period` bước đầu."""
    if len(closes) < period + 1:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = sum(max(d, 0) for d in deltas[:period]) / period
    loss = sum(max(-d, 0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def evaluate(closes: list[float]) -> dict[str, Any]:
    """Ba tín hiệu trên chuỗi close 15m (cũ → mới). `fired` = danh sách tín hiệu đang bật kèm hướng gợi."""
    pb = bollinger_pct_b(closes)
    st = streak(closes)
    r9 = rsi(closes, 9)
    fired: list[dict[str, str]] = []
    if pb is not None and pb < 0:
        fired.append({"name": "bollinger", "hint": "up"})
    elif pb is not None and pb > 100:
        fired.append({"name": "bollinger", "hint": "down"})
    if st <= -STREAK_MIN:
        fired.append({"name": "streak", "hint": "up"})
    elif st >= STREAK_MIN:
        fired.append({"name": "streak", "hint": "down"})
    if r9 is not None and r9 < 20:
        fired.append({"name": "rsi9", "hint": "up"})
    elif r9 is not None and r9 > 80:
        fired.append({"name": "rsi9", "hint": "down"})
    ups = sum(f["hint"] == "up" for f in fired)
    downs = len(fired) - ups
    return {
        "pct_b": round(pb, 1) if pb is not None else None,
        "streak": st,
        "rsi9": round(r9, 1) if r9 is not None else None,
        "fired": fired,
        "has_signal": bool(fired),
        "hint": "up" if ups > downs else "down" if downs > ups else ("mixed" if fired else None),
        "enough_bars": len(closes) >= 21,
    }


def auto_trigger(ev: dict[str, Any]) -> bool:
    """Tín hiệu đủ mạnh để tạo dự đoán tự động: ≥ AUTO_MIN_FIRED tín hiệu bật và cùng một hướng."""
    return len(ev["fired"]) >= AUTO_MIN_FIRED and ev["hint"] in ("up", "down")


def bar_boundary(now: datetime) -> datetime:
    """Mốc đóng nến 15 phút gần nhất ≤ now (UTC)."""
    return now.replace(minute=now.minute - now.minute % BAR_MINUTES, second=0, microsecond=0)


def auto_allowed(bar_end: datetime) -> bool:
    """Dự đoán tự động chỉ khi thị trường mở suốt [mốc, mốc + 15 phút + 5 phút chấm] — không tạo trong 15 phút
    cuối trước giờ nghỉ hằng ngày / cuối tuần. Giờ nghỉ dài 60 phút nên xét 2 đầu là đủ."""
    return is_market_open(bar_end) and is_market_open(bar_end + timedelta(minutes=BAR_MINUTES + 5))


async def compute(session: AsyncSession, end: datetime) -> dict[str, Any]:
    """Tín hiệu trên 60 nến 15m sống đã đóng trước `end`. Kèm 24 nến gần nhất cho gói dữ liệu."""
    bars = await candle_repo.live_bars(session, BAR_MINUTES, end, end - LOOKBACK, N_BARS)
    closes = [b["close"] for b in bars]
    out = evaluate(closes)
    last = bars[-1] if bars else None
    out.update(
        {
            "bar_end": end.isoformat(),
            "last_bar_ts": last["ts"].isoformat() if last else None,
            "last_close": last["close"] if last else None,
            "bars": len(bars),
            "recent": [
                {
                    "vn": b["ts"].astimezone(VN).strftime("%d/%m %H:%M"),
                    "o": round(b["open"], 2),
                    "h": round(b["high"], 2),
                    "l": round(b["low"], 2),
                    "c": round(b["close"], 2),
                }
                for b in bars[-24:]
            ],
        }
    )
    return out


async def vol96(session: AsyncSession, end: datetime) -> float | None:
    """(v3) TB |Δ close| của 96 nến 15m sống gần nhất trước `end` — biến động 1 ngày, luật B tài khoản giấy
    (cùng định nghĩa research/paper_filters.py)."""
    bars = await candle_repo.live_bars(session, BAR_MINUTES, end, end - timedelta(days=7), 97)
    c = [b["close"] for b in bars]
    if len(c) < 97:
        return None
    return round(sum(abs(c[i] - c[i - 1]) for i in range(1, len(c))) / 96, 4)


async def context(session: AsyncSession, end: datetime, price: float | None) -> dict[str, Any]:
    """Ngữ cảnh không dùng để đoán: vị trí trong biên 5/20/60 ngày, RSI 14 khung 1h/4h/1D (nến đã đóng)."""
    ref = price
    ranges: dict[str, Any] = {}
    for days in (5, 20, 60):
        lo, hi = await candle_repo.live_range(session, end - timedelta(days=days), end)
        pos = None
        if ref is not None and lo is not None and hi is not None and hi > lo:
            pos = round((ref - lo) / (hi - lo) * 100, 1)
        ranges[f"{days}d"] = {"low": lo, "high": hi, "position_pct": pos}
    rsi14: dict[str, float | None] = {}
    for label, minutes, lookback in (("1h", 60, 10), ("4h", 240, 30), ("1d", 1440, 120)):
        bars = await candle_repo.live_bars(session, minutes, end, end - timedelta(days=lookback), 200)
        v = rsi([b["close"] for b in bars], 14)
        rsi14[label] = round(v, 1) if v is not None else None
    return {"range": ranges, "rsi14": rsi14}
