"""Đặc trưng giá cho mô hình 15 phút — BẢN THAM CHIẾU (pandas). Bản production thuần Python:
`src/goldpulse/services/model15m.py`; `research/parity_model.py` đối chiếu hai bản từng nến."""

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
FEATURES = [
    "r1", "r2", "r4", "r8", "r16", "r32", "r96", "pctb", "rsi9", "rsi14", "streak", "upwick", "lowick", "body",
    "range_atr", "vol_regime", "hsin", "hcos", "dow", "d_prevhi", "d_prevlo", "d_dayopen", "round10",
]  # fmt: skip
ATR_N = 96
VOL_WIN = 96 * 20


def market_open(ts: pd.Series) -> np.ndarray:
    ny = ts.dt.tz_convert(NY)
    dow, hr = ny.dt.weekday, ny.dt.hour
    return (~((hr == 17) | (dow == 5) | ((dow == 4) & (hr >= 17)) | ((dow == 6) & (hr < 18)))).values


def rsi(x: pd.Series, n: int) -> np.ndarray:
    d = x.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    g, l_ = up.ewm(alpha=1 / n, adjust=False).mean(), dn.ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + g / l_)).values


def build(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """df: ts (UTC), open, high, low, close — nến 15m có giao dịch. Trả (df đã lọc giờ mở, bảng đặc trưng)."""
    df = df.sort_values("ts").reset_index(drop=True)
    df = df[market_open(df["ts"])].reset_index(drop=True)
    c, o, h, lo = (df[k].values for k in ("close", "open", "high", "low"))
    ts = df["ts"]
    s = pd.Series(c)
    pc = np.roll(c, 1)
    tr = pd.Series(np.maximum.reduce([h - lo, np.abs(h - pc), np.abs(lo - pc)]))
    atr = tr.rolling(ATR_N).mean().values
    F = pd.DataFrame(index=df.index)
    for k in (1, 2, 4, 8, 16, 32, 96):
        F[f"r{k}"] = (s - s.shift(k)).values / atr
    sma20, sd20 = s.rolling(20).mean(), s.rolling(20).std(ddof=0)
    F["pctb"] = ((s - (sma20 - 2 * sd20)) / (4 * sd20)).values
    F["rsi9"], F["rsi14"] = rsi(s, 9), rsi(s, 14)
    sign = np.sign(np.diff(c, prepend=np.nan))
    streak = np.zeros(len(c))
    for i in range(1, len(c)):
        prev = streak[i - 1]
        streak[i] = prev + sign[i] if sign[i] != 0 and np.sign(prev) in (0, sign[i]) else sign[i]
    F["streak"] = streak
    rng_ = np.maximum(h - lo, 1e-9)
    F["upwick"] = (h - np.maximum(o, c)) / rng_
    F["lowick"] = (np.minimum(o, c) - lo) / rng_
    F["body"] = (c - o) / rng_
    F["range_atr"] = (h - lo) / atr
    F["vol_regime"] = pd.Series(atr).rolling(VOL_WIN).rank(pct=True).values
    utc_h = ts.dt.hour + ts.dt.minute / 60
    F["hsin"], F["hcos"] = np.sin(2 * np.pi * utc_h / 24), np.cos(2 * np.pi * utc_h / 24)
    F["dow"] = ts.dt.weekday.values
    day = ts.dt.tz_convert(NY).dt.date
    g = df.groupby(day)
    daily = pd.DataFrame({"hi": g["high"].max(), "lo": g["low"].min()}).shift(1)
    F["d_prevhi"] = (c - day.map(daily["hi"]).values) / atr
    F["d_prevlo"] = (c - day.map(daily["lo"]).values) / atr
    F["d_dayopen"] = (c - g["open"].transform("first").values) / atr
    F["round10"] = (c % 10) / 10
    return df, F[FEATURES]
