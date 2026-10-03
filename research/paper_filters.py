import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "research")
from features import build  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
SPREAD = 0.30
raw = pd.read_csv("research/bars15.csv", parse_dates=["ts"])
df, F = build(raw)
md = json.load(open("data/models/logit15m_v1.json", encoding="utf-8"))
FV = F.values
Z = (FV - np.array(md["mean"])) / np.array(md["scale"])
P = 1 / (1 + np.exp(-(Z @ np.array(md["coef"]) + md["intercept"])))
ok = np.isfinite(FV).all(axis=1)
trig = ok & (np.abs(P - 0.5) >= md["confidence_threshold"])
ts = df["ts"]
gap_next = (ts.shift(-1) - ts).dt.total_seconds().values
c = df["close"].values
yr = ts.dt.year.values
idx = [i for i in np.where(trig)[0] if i + 1 < len(c) and gap_next[i] == 900]
T = pd.DataFrame({"i": idx})
T["dir"] = np.where(P[idx] > 0.5, 1, -1)
T["conf"] = np.abs(P[idx] - 0.5)
T["net"] = T["dir"] * (c[[i + 1 for i in idx]] - c[idx]) - SPREAD
T["yr"] = yr[idx]
absd = pd.Series(np.abs(np.diff(c, prepend=np.nan)))
T["vol"] = absd.rolling(96).mean().values[idx]  # biến động 1 ngày trước, biết lúc vào lệnh
B, C = T[T["yr"].between(2022, 2024)], T[T["yr"] >= 2025]

print("lãi ròng/lệnh/oz sau spread theo ngưỡng biến động đã biết (TB |Δ15m| 96 nến trước)")
for th in (0, 1, 1.5, 2, 3, 4, 5, 6):
    b, cc = B[B.vol >= th], C[C.vol >= th]
    print(
        f"vol ≥ {th:>3}: 2022–24 {len(b):>5} lệnh net {b.net.mean():+.3f} · "
        f"2025–26 {len(cc):>4} lệnh net {cc.net.mean():+.3f} · thắng sau phí {(cc.net > 0).mean():.1%}"
    )
print("\ntheo mức tự tin của mô hình (top phân vị trong nến trigger)")
for q in (0, 0.5, 0.75, 0.9):
    thr = B.conf.quantile(q)
    b, cc = B[B.conf >= thr], C[C.conf >= thr]
    print(f"conf ≥ q{q}: 2022–24 {len(b):>5} net {b.net.mean():+.3f} · 2025–26 {len(cc):>4} net {cc.net.mean():+.3f}")
print("\ntheo năm")
for y in range(2012, 2027):
    s = T[T.yr == y]
    if len(s):
        print(
            y,
            f"{len(s):>4} lệnh · vol TB {s.vol.mean():.2f} · trước phí {(s.net + SPREAD).mean():+.3f} · sau phí {s.net.mean():+.3f}",
        )

# Luật D — giữ 4 nến (1 giờ) thay vì 1 nến, cùng spread
gap4 = (ts.shift(-4) - ts).dt.total_seconds().values
ok4 = np.array([gap4[i] == 3600 for i in idx])
T["net1h"] = np.where(ok4, T["dir"] * (c[[min(i + 4, len(c) - 1) for i in idx]] - c[idx]) - SPREAD, np.nan)
print("\nluật D — giữ 1 giờ (chỉ lệnh có 4 nến liền kề)")
for name, S in (("2022–24", T[T.yr.between(2022, 2024)]), ("2025–26", T[T.yr >= 2025])):
    s = S.dropna(subset=["net1h"])
    print(f"{name}: {len(s)} lệnh · sau phí {s.net1h.mean():+.3f} · đúng hướng {((s.net1h + SPREAD) > 0).mean():.1%}")
print(
    "ngưỡng tự tin q0.5 (2022–24):",
    round(float(B.conf.quantile(0.5)), 4),
    "· ngưỡng mô hình:",
    md["confidence_threshold"],
)
