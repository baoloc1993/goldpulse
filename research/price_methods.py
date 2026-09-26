"""Nghiên cứu phương pháp dự đoán hướng nến 15 phút kế tiếp chỉ từ giá — kỷ luật 3 giai đoạn.

A = 2012–2021 (tìm/luyện) · B = 2022–2024 (chọn ngưỡng) · C = 2025 → 09/2026 (kiểm cuối, chỉ nhìn một lần).
Nhãn: close nến kế tiếp > close nến này (nến kế tiếp phải liền kề, cả hai trong giờ thị trường mở); |Δ| < 0,01 loại.
"""

import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.stdout.reconfigure(encoding="utf-8")
NY = ZoneInfo("America/New_York")
df = pd.read_csv(sys.argv[1], parse_dates=["ts"])
df = df.sort_values("ts").reset_index(drop=True)

# ---- lịch thị trường (như services/calendar.is_market_open) ----
ny = df["ts"].dt.tz_convert(NY)
dow, hr = ny.dt.weekday, ny.dt.hour
open_ = ~((hr == 17) | (dow == 5) | ((dow == 4) & (hr >= 17)) | ((dow == 6) & (hr < 18)))
df = df[open_.values].reset_index(drop=True)

c, o, h, lo = df["close"].values, df["open"].values, df["high"].values, df["low"].values
ts = df["ts"]
gap_next = (ts.shift(-1) - ts).dt.total_seconds().values
gap_prev = (ts - ts.shift(1)).dt.total_seconds().values
ret = np.diff(c, prepend=np.nan)
nxt = np.roll(c, -1) - c
valid = (gap_next == 900) & (np.abs(nxt) >= 0.01)
y = (nxt > 0).astype(int)

# ---- đặc trưng ----
s = pd.Series(c)
pc = np.roll(c, 1)
tr = pd.Series(np.maximum.reduce([h - lo, np.abs(h - pc), np.abs(lo - pc)]))
atr = tr.rolling(96).mean().values  # ~1 ngày
F = pd.DataFrame(index=df.index)
for k in (1, 2, 4, 8, 16, 32, 96):
    F[f"r{k}"] = (s - s.shift(k)).values / atr
sma20, sd20 = s.rolling(20).mean(), s.rolling(20).std(ddof=0)
F["pctb"] = ((s - (sma20 - 2 * sd20)) / (4 * sd20)).values


def rsi(x, n):
    d = x.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    g, l_ = up.ewm(alpha=1 / n, adjust=False).mean(), dn.ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + g / l_)).values


F["rsi9"], F["rsi14"] = rsi(s, 9), rsi(s, 14)
sign = np.sign(ret)
streak = np.zeros(len(c))
for i in range(1, len(c)):
    streak[i] = streak[i - 1] + sign[i] if sign[i] != 0 and np.sign(streak[i - 1]) in (0, sign[i]) else sign[i]
F["streak"] = streak
rng_ = np.maximum(h - lo, 1e-9)
F["upwick"] = (h - np.maximum(o, c)) / rng_
F["lowick"] = (np.minimum(o, c) - lo) / rng_
F["body"] = (c - o) / rng_
F["range_atr"] = (h - lo) / atr
F["vol_regime"] = pd.Series(atr).rolling(96 * 20).rank(pct=True).values
utc_h = ts.dt.hour + ts.dt.minute / 60
F["hsin"], F["hcos"] = np.sin(2 * np.pi * utc_h / 24), np.cos(2 * np.pi * utc_h / 24)
F["dow"] = ts.dt.weekday
day = ts.dt.tz_convert(NY).dt.date  # "ngày giao dịch" xấp xỉ theo NY
g = df.groupby(day)
dhi, dlo, dopen = g["high"].transform("max"), g["low"].transform("min"), g["open"].transform("first")
# đỉnh/đáy ngày TRƯỚC (không nhìn trước): dịch theo ngày
daily = pd.DataFrame({"hi": g["high"].max(), "lo": g["low"].min()}).shift(1)
F["d_prevhi"] = (c - day.map(daily["hi"]).values) / atr
F["d_prevlo"] = (c - day.map(daily["lo"]).values) / atr
F["d_dayopen"] = (c - dopen.values) / atr
F["round10"] = (c % 10) / 10
period = np.where(ts.dt.year <= 2021, "A", np.where(ts.dt.year <= 2024, "B", "C"))
ok = valid & (ts.dt.year >= 2012).values & (gap_prev == 900) & np.isfinite(F.values).all(axis=1) & np.isfinite(atr)
P = {p: ok & (period == p) for p in "ABC"}
print("mẫu:", {p: int(P[p].sum()) for p in "ABC"}, "· tỉ lệ nến tăng:", {p: round(100 * y[P[p]].mean(), 1) for p in "ABC"})


def show(name, pred, mask=None):
    """pred: +1 lên / -1 xuống / 0 không đoán. In độ phủ + tỉ lệ đúng từng giai đoạn."""
    out = []
    for p in "ABC":
        m = P[p] & (pred != 0) & (True if mask is None else mask)
        n = int(m.sum())
        acc = 100 * ((pred[m] > 0) == (y[m] == 1)).mean() if n else float("nan")
        cov = 100 * n / P[p].sum()
        se = 196 * np.sqrt(acc / 100 * (1 - acc / 100) / n) if n else float("nan")
        out.append(f"{p}: {acc:5.1f}% ±{se:3.1f} (phủ {cov:4.1f}%, n={n})")
    print(f"{name:42} " + " | ".join(out))


print("\n== 1. Luật đơn giản ==")
one = np.ones(len(c))
show("luôn đoán tăng", one)
for k in (1, 4, 16, 96):
    r = F[f"r{k}"].values
    show(f"quán tính {k * 15} phút (theo hướng)", np.sign(r))
    show(f"đảo chiều {k * 15} phút (ngược hướng)", -np.sign(r))

print("\n== 2. Tín hiệu hiện tại (production) ==")
pb, r9, st = F["pctb"].values * 100, F["rsi9"].values, F["streak"].values
ups = (pb < 0).astype(int) + (st <= -6) + (r9 < 20)
dns = (pb > 100).astype(int) + (st >= 6) + (r9 > 80)
sig1 = np.where((ups >= 1) & (dns == 0), 1, np.where((dns >= 1) & (ups == 0), -1, 0))
sig2 = np.where((ups >= 2) & (dns == 0), 1, np.where((dns >= 2) & (ups == 0), -1, 0))
show("≥1 tín hiệu", sig1)
show("≥2 tín hiệu", sig2)
show("≥2 tín hiệu, chỉ chiều lên", np.where(sig2 > 0, 1, 0))
show("3 tín hiệu cùng bật", np.where(ups == 3, 1, np.where(dns == 3, -1, 0)))

print("\n== 3. Giờ trong ngày / thứ (học trên A, áp B, C) ==")
hour = ts.dt.hour.values
bias = {}
for hh in range(24):
    m = P["A"] & (hour == hh)
    bias[hh] = y[m].mean() if m.sum() > 500 else 0.5
hb = np.array([bias[x] for x in hour])
for thr in (0.52, 0.53, 0.54):
    show(f"giờ UTC lệch ≥ {thr:.2f} trên A", np.where(hb >= thr, 1, np.where(hb <= 1 - thr, -1, 0)))
wk = F["dow"].values
bias2 = {}
for d in range(7):
    for hh in range(24):
        m = P["A"] & (hour == hh) & (wk == d)
        bias2[(d, hh)] = y[m].mean() if m.sum() > 300 else 0.5
hb2 = np.array([bias2[(a, b)] for a, b in zip(wk, hour, strict=False)])
show("thứ×giờ lệch ≥ 0.55 trên A", np.where(hb2 >= 0.55, 1, np.where(hb2 <= 0.45, -1, 0)))

print("\n== 4. Mẫu nến / phá vỡ / chế độ biến động ==")
lw, uw, rga = F["lowick"].values, F["upwick"].values, F["range_atr"].values
show("râu dưới dài (>0.6, nến lớn) → lên", np.where((lw > 0.6) & (rga > 1.5), 1, 0))
show("râu trên dài (>0.6, nến lớn) → xuống", np.where((uw > 0.6) & (rga > 1.5), -1, 0))
dph, dpl = F["d_prevhi"].values, F["d_prevlo"].values
show("vừa vượt đỉnh ngày trước → tiếp diễn lên", np.where((dph > 0) & (dph < 0.3), 1, 0))
show("vừa thủng đáy ngày trước → tiếp diễn xuống", np.where((dpl < 0) & (dpl > -0.3), -1, 0))
show("vừa vượt đỉnh ngày trước → đảo xuống", np.where((dph > 0) & (dph < 0.3), -1, 0))
vr = F["vol_regime"].values
show("≥2 tín hiệu khi biến động THẤP (<40%)", sig2, vr < 0.4)
show("≥2 tín hiệu khi biến động CAO (>60%)", sig2, vr > 0.6)
show("nến rất lớn (>3 ATR) → đảo chiều", np.where(rga > 3, -np.sign(F["body"].values), 0))

print("\n== 5. Học máy: luyện A, chọn ngưỡng B, kiểm C ==")
X = F.values
Xa, ya = X[P["A"]], y[P["A"]]
sc = StandardScaler().fit(Xa)
models = {
    "hồi quy logistic": LogisticRegression(max_iter=2000, C=0.1).fit(sc.transform(Xa), ya),
    "cây tăng cường": HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                                     l2_regularization=1.0, random_state=0).fit(Xa, ya),
}  # fmt: skip
for name, mdl in models.items():
    Xf = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)  # nến ngoài mẫu (ok=False) bị che ở show()
    prob = mdl.predict_proba(sc.transform(Xf) if "logistic" in name else Xf)[:, 1]
    show(f"{name}: mọi nến", np.where(prob >= 0.5, 1, -1))
    for q in (0.10, 0.05, 0.02):
        # ngưỡng tự tin chọn trên B (không nhìn C): top q theo |p − 0.5|
        conf = np.abs(prob - 0.5)
        thr = np.quantile(conf[P["B"]], 1 - q)
        show(f"{name}: {int(q * 100)}% tự tin nhất (ngưỡng từ B)", np.where(conf >= thr, np.sign(prob - 0.5), 0))
