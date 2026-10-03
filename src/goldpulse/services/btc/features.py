"""(v4) Đặc trưng nến 1h BTC — Python thuần, khớp `research/btc_methods.py::features` (pandas) từng hàng.

Quy ước pandas được chép lại có chủ đích (lệch quy ước là lỗi im lặng nguy hiểm nhất — DECISIONS 2026-09-26):
- rolling(n): cần đủ n giá trị không NaN, độ lệch chuẩn mẫu ddof = 1, quantile nội suy tuyến tính
- ewm(alpha, adjust=False) cho RSI: khởi từ giá trị không NaN đầu tiên; ewm(span, adjust=True) cho EMA 24h
- chia cho 0 / log(0) → inf → coi là NaN (`replace([inf, -inf], nan)`)
- tính theo THỨ TỰ hàng, không theo giờ: giờ không có nến đã bị bỏ từ trước (challenge I 2026-10-03)
"""

import math
from collections.abc import Sequence
from typing import Any

DAY = 24
F = float | None


def _log(x: F) -> F:
    if x is None or not math.isfinite(x) or x <= 0:
        return None
    return math.log(x)


def _div(a: F, b: F) -> F:
    if a is None or b is None or b == 0:
        return None
    r = a / b
    return r if math.isfinite(r) else None


def rolling(xs: Sequence[F], n: int, fn) -> list[F]:
    out: list[F] = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1 : i + 1]
        if any(v is None for v in w):
            continue
        out[i] = fn(w)
    return out


def _mean(w: Sequence[float]) -> float:
    return math.fsum(w) / len(w)


def _std(w: Sequence[float]) -> float:
    m = _mean(w)
    return math.sqrt(math.fsum((v - m) ** 2 for v in w) / (len(w) - 1))


def quantile(w: Sequence[float], q: float) -> float:
    s = sorted(w)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def ewm_adjust_false(xs: Sequence[F], alpha: float) -> list[F]:
    out: list[F] = [None] * len(xs)
    prev: F = None
    for i, x in enumerate(xs):
        if x is None:
            out[i] = prev
            continue
        prev = x if prev is None else (1 - alpha) * prev + alpha * x
        out[i] = prev
    return out


def ewm_span_adjust(xs: Sequence[float], span: int) -> list[float]:
    a = 2 / (span + 1)
    num = den = 0.0
    out = []
    for x in xs:
        num = x + (1 - a) * num
        den = 1 + (1 - a) * den
        out.append(num / den)
    return out


def rsi(close: Sequence[float], n: int) -> list[F]:
    d: list[F] = [None] + [close[i] - close[i - 1] for i in range(1, len(close))]
    g = ewm_adjust_false([None if v is None else max(v, 0.0) for v in d], 1 / n)
    lo = ewm_adjust_false([None if v is None else max(-v, 0.0) for v in d], 1 / n)
    out: list[F] = []
    for gi, li in zip(g, lo, strict=True):
        if gi is None or li is None or (gi == 0 and li == 0):
            out.append(None)
        elif li == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + gi / li))
    return out


def compute(bars: Sequence[dict[str, Any]]) -> list[dict[str, F]]:
    """bars: nến 1h theo thời gian (bar_ts, open, high, low, close, volume). Trả mỗi hàng một dict đặc trưng (None =
    NaN) + vài số phụ cho chiến thuật (`_sigma`, `_m20`, `_s20`, `_width`)."""
    n = len(bars)
    c = [float(b["close"]) for b in bars]
    o = [float(b["open"]) for b in bars]
    h = [float(b["high"]) for b in bars]
    lo = [float(b["low"]) for b in bars]
    v = [float(b["volume"]) for b in bars]
    lr: list[F] = [None] + [_log(c[i] / c[i - 1]) for i in range(1, n)]
    vol = rolling(lr, DAY, _std)
    vol_long = rolling(lr, DAY * 20, _std)
    m20 = rolling(c, 20, _mean)
    s20 = rolling(c, 20, _std)
    rsi9, rsi14 = rsi(c, 9), rsi(c, 14)
    vmean = rolling(v, DAY, _mean)
    hmax = rolling(h, DAY, max)
    lmin = rolling(lo, DAY, min)
    ema = ewm_span_adjust(c, DAY)
    width = [_div(4 * s, m) if s is not None and m is not None else None for s, m in zip(s20, m20, strict=True)]
    out: list[dict[str, F]] = []
    for i, b in enumerate(bars):
        f: dict[str, F] = {}
        for k in (1, 2, 4, 8, 16, 32):
            f[f"r{k}"] = _log(c[i] / c[i - k]) if i >= k else None
        f["rday"] = _log(c[i] / c[i - DAY]) if i >= DAY else None
        f["vol"] = vol[i]
        f["vol_regime"] = _div(vol[i], vol_long[i])
        f["pctb"] = (
            _div(c[i] - (m20[i] - 2 * s20[i]), 4 * s20[i]) if m20[i] is not None and s20[i] is not None else None
        )
        f["rsi9"], f["rsi14"] = rsi9[i], rsi14[i]
        rng = h[i] - lo[i]
        f["body"] = _div(c[i] - o[i], rng)
        f["upwick"] = _div(h[i] - max(c[i], o[i]), rng)
        f["lowick"] = _div(min(c[i], o[i]) - lo[i], rng)
        f["range_vol"] = _div(_log(h[i] / lo[i]) if lo[i] > 0 else None, vol[i])
        f["vratio"] = _log(_div(v[i], vmean[i]))
        f["d_hi"] = _log(c[i] / hmax[i]) if hmax[i] else None
        f["d_lo"] = _log(c[i] / lmin[i]) if lmin[i] else None
        f["ema_gap"] = _log(c[i] / ema[i])
        hr = b["bar_ts"].hour
        f["hsin"], f["hcos"] = math.sin(2 * math.pi * hr / 24), math.cos(2 * math.pi * hr / 24)
        f["wkend"] = 1.0 if b["bar_ts"].weekday() >= 5 else 0.0
        f["_r1"] = lr[i]
        f["_sigma"] = vol[i]
        f["_m20"], f["_s20"], f["_width"] = m20[i], s20[i], width[i]
        out.append(f)
    return out
