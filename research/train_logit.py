"""Luyện mô hình logistic 15 phút + xuất JSON cho production (chủ dự án chốt 2026-09-26: chỉ dự đoán 2% tự tin nhất).

Luyện 2012–2021 · ngưỡng tự tin = phân vị 98% của |p − 0,5| trên 2022–2024 · báo lại số trên 2025–2026 (đã nhìn ở
nghiên cứu, không chỉnh thêm). Đúng cấu hình đã kiểm trong `price_methods.py`.

    python -m uv run --no-project --with numpy --with pandas --with scikit-learn python research/train_logit.py \
        research/bars15.csv data/models/logit15m_v1.json
"""

import json
import sys
from datetime import date

import numpy as np
import pandas as pd
from features import FEATURES, build
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

TOP = 0.02
raw = pd.read_csv(sys.argv[1], parse_dates=["ts"])
df, F = build(raw)
ts = df["ts"]
c = df["close"].values
gap_next = (ts.shift(-1) - ts).dt.total_seconds().values
gap_prev = (ts - ts.shift(1)).dt.total_seconds().values
nxt = np.roll(c, -1) - c
y = (nxt > 0).astype(int)
ok = (gap_next == 900) & (gap_prev == 900) & (np.abs(nxt) >= 0.01) & np.isfinite(F.values).all(axis=1)
ok &= (ts.dt.year >= 2012).values
yr = ts.dt.year.values
A, B, C = ok & (yr <= 2021), ok & (yr >= 2022) & (yr <= 2024), ok & (yr >= 2025)
X = F.values
sc = StandardScaler().fit(X[A])
m = LogisticRegression(max_iter=2000, C=0.1).fit(sc.transform(X[A]), y[A])
p = m.predict_proba(sc.transform(np.nan_to_num(X)))[:, 1]
conf = np.abs(p - 0.5)
thr = float(np.quantile(conf[B], 1 - TOP))


def acc(mask):
    sel = mask & (conf >= thr)
    return round(100 * ((p[sel] > 0.5) == (y[sel] == 1)).mean(), 2), int(sel.sum()), round(100 * sel.sum() / mask.sum(), 2)


metrics = {k: dict(zip(("accuracy_pct", "n", "coverage_pct"), acc(v), strict=True)) for k, v in
           (("train_2012_2021", A), ("select_2022_2024", B), ("test_2025_2026", C))}  # fmt: skip
model = {
    "name": "logit15m_v1",
    "created": date.today().isoformat(),
    "target": "hướng close nến 15m kế tiếp so close nến vừa đóng (nến có giao dịch, giờ thị trường mở)",
    "features": FEATURES,
    "mean": sc.mean_.tolist(),
    "scale": sc.scale_.tolist(),
    "coef": m.coef_[0].tolist(),
    "intercept": float(m.intercept_[0]),
    "confidence_threshold": thr,
    "top_fraction": TOP,
    "trained_on": "2012-01-01 → 2021-12-31",
    "threshold_from": "2022-01-01 → 2024-12-31",
    "metrics": metrics,
    "source": "research/train_logit.py + research/features.py",
}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(model, f, ensure_ascii=False, indent=1)
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps(metrics, ensure_ascii=False), "ngưỡng", round(thr, 5))
for name, w in sorted(zip(FEATURES, m.coef_[0], strict=True), key=lambda t: -abs(t[1]))[:8]:
    print(f"  {name:11} {w:+.3f}")
