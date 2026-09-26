"""Mô hình logistic dự đoán hướng nến 15 phút kế tiếp (chủ dự án chốt 2026-09-26: chỉ dự đoán 2% tự tin nhất).

Thuần Python, không numpy — tính ĐÚNG như bản tham chiếu `research/features.py` (đối chiếu từng nến:
`research/parity_model.py`). Hệ số + chuẩn hoá + ngưỡng: `data/models/logit15m_v1.json` (luyện 2012–2021, ngưỡng từ
2022–2024; kiểm 2025–2026: 56,7% trên 977 lần, phủ 2,4% số nến).
"""

import json
import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import candles as candle_repo
from .calendar import NY, is_market_open

MODEL_FILE = Path(__file__).resolve().parents[3] / "data" / "models" / "logit15m_v1.json"
ATR_N = 96
VOL_WIN = 96 * 20
LOOKBACK = timedelta(days=45)  # ≥ VOL_WIN + ATR_N nến giờ mở (~23 ngày giao dịch) + đệm cuối tuần/lễ
MIN_BARS = VOL_WIN + ATR_N + 1


@lru_cache(maxsize=1)
def model() -> dict[str, Any]:
    return json.loads(MODEL_FILE.read_text(encoding="utf-8"))


def _ewm_rsi(closes: list[float], n: int) -> float:
    """RSI theo pandas `ewm(alpha=1/n, adjust=False)` — bắt đầu từ bước chênh lệch đầu tiên."""
    a = 1 / n
    g = l_ = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up, dn = max(d, 0.0), max(-d, 0.0)
        if g is None:
            g, l_ = up, dn
        else:
            g = (1 - a) * g + a * up
            l_ = (1 - a) * l_ + a * dn
    if g is None or l_ is None:
        return float("nan")
    if l_ == 0:
        return 100.0 if g > 0 else float("nan")
    return 100 - 100 / (1 + g / l_)


def features(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Đặc trưng của nến CUỐI trong `bars` (nến 15m có giao dịch, cũ → mới, đã lọc giờ thị trường mở).
    None khi chưa đủ lịch sử."""
    if len(bars) < MIN_BARS:
        return None
    c = [b["close"] for b in bars]
    o = [b["open"] for b in bars]
    h = [b["high"] for b in bars]
    lo = [b["low"] for b in bars]
    n = len(bars)
    tr = [max(h[j] - lo[j], abs(h[j] - c[j - 1]), abs(lo[j] - c[j - 1])) for j in range(1, n)]
    tr.insert(0, h[0] - lo[0])
    atrs = [sum(tr[j - ATR_N + 1 : j + 1]) / ATR_N for j in range(ATR_N - 1, n)]  # atrs[-1] = ATR tại nến cuối
    atr = atrs[-1]
    i = n - 1
    f: dict[str, float] = {}
    for k in (1, 2, 4, 8, 16, 32, 96):
        f[f"r{k}"] = (c[i] - c[i - k]) / atr
    win = c[-20:]
    m = sum(win) / 20
    sd = math.sqrt(sum((x - m) ** 2 for x in win) / 20)
    f["pctb"] = (c[i] - (m - 2 * sd)) / (4 * sd) if sd else float("nan")
    f["rsi9"], f["rsi14"] = _ewm_rsi(c, 9), _ewm_rsi(c, 14)
    st = 0.0
    for j in range(1, n):
        d = c[j] - c[j - 1]
        s = (d > 0) - (d < 0)
        st = st + s if s != 0 and (st == 0 or (st > 0) == (s > 0)) else float(s)
    f["streak"] = st
    rng = max(h[i] - lo[i], 1e-9)
    f["upwick"] = (h[i] - max(o[i], c[i])) / rng
    f["lowick"] = (min(o[i], c[i]) - lo[i]) / rng
    f["body"] = (c[i] - o[i]) / rng
    f["range_atr"] = (h[i] - lo[i]) / atr
    window = atrs[-VOL_WIN:]
    less = sum(1 for x in window if x < atr)
    equal = sum(1 for x in window if x == atr)
    f["vol_regime"] = (less + (equal + 1) / 2) / VOL_WIN  # pandas rank(pct=True), method="average"
    ts: datetime = bars[i]["ts"]
    hh = ts.hour + ts.minute / 60
    f["hsin"], f["hcos"] = math.sin(2 * math.pi * hh / 24), math.cos(2 * math.pi * hh / 24)
    f["dow"] = float(ts.weekday())
    days: dict[date, list[int]] = {}
    for j, b in enumerate(bars):
        days.setdefault(b["ts"].astimezone(NY).date(), []).append(j)
    today = ts.astimezone(NY).date()
    prev = [d for d in days if d < today]
    if not prev:
        return None
    pd_idx = days[max(prev)]
    f["d_prevhi"] = (c[i] - max(h[j] for j in pd_idx)) / atr
    f["d_prevlo"] = (c[i] - min(lo[j] for j in pd_idx)) / atr
    f["d_dayopen"] = (c[i] - o[days[today][0]]) / atr
    f["round10"] = (c[i] % 10) / 10
    if not all(math.isfinite(v) for v in f.values()):
        return None
    return f


def predict(f: dict[str, float]) -> dict[str, Any]:
    md = model()
    z = md["intercept"] + sum(
        w * (f[name] - mu) / sd
        for name, w, mu, sd in zip(md["features"], md["coef"], md["mean"], md["scale"], strict=True)
    )
    p = 1 / (1 + math.exp(-z))
    conf = abs(p - 0.5)
    return {
        "model": md["name"],
        "p_up": round(p, 4),
        "direction": "up" if p > 0.5 else "down",
        "confidence": round(conf, 4),
        "threshold": md["confidence_threshold"],
        "trigger": conf >= md["confidence_threshold"],
    }


async def evaluate(session: AsyncSession, bar_end: datetime) -> dict[str, Any] | None:
    """Mô hình tại mốc `bar_end`: chỉ nến đã đóng trước mốc, chỉ giờ thị trường mở. None khi thiếu lịch sử."""
    bars = await candle_repo.live_bars(session, 15, bar_end, bar_end - LOOKBACK, 10_000)
    bars = [b for b in bars if is_market_open(b["ts"])]
    f = features(bars)
    if f is None:
        return None
    return {**predict(f), "features": {k: round(v, 4) for k, v in f.items()}}
