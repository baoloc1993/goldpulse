"""Đối chiếu từng nến: bản tham chiếu (pandas, research/features.py) vs bản production (services/model15m.py).

    PYTHONPATH=src python -m uv run --no-project --with numpy --with pandas --with scikit-learn --with sqlalchemy \
        --with asyncpg --with pydantic-settings --with structlog python research/parity_model.py research/bars15.csv
(hoặc chạy bằng .venv của dự án + `uv run --with pandas`). Lệch xác suất > 0,001 hoặc lệch quyết định trigger → báo.
"""

import random
import sys
from datetime import timedelta

import numpy as np
import pandas as pd
from features import FEATURES, build

from goldpulse.services import model15m

sys.stdout.reconfigure(encoding="utf-8")
raw = pd.read_csv(sys.argv[1], parse_dates=["ts"])
df, F = build(raw)
md = model15m.model()
FV = F.values  # chép MỘT lần — F.values trong vòng lặp chép cả bảng mỗi lượt
Z = (FV - np.array(md["mean"])) / np.array(md["scale"])
P = 1 / (1 + np.exp(-(Z @ np.array(md["coef"]) + md["intercept"])))
yr = df["ts"].dt.year.values
cand = [i for i in range(len(df)) if yr[i] >= 2025 and np.isfinite(FV[i]).all()]
rng = random.Random(7)
# lấy cả nến trigger (quan trọng nhất) lẫn nến thường
trig = [i for i in cand if abs(P[i] - 0.5) >= md["confidence_threshold"]]
NPICK = int(sys.argv[2]) if len(sys.argv) > 2 else 200
picks = rng.sample(trig, min(NPICK // 3, len(trig))) + rng.sample(cand, NPICK - NPICK // 3)
print("đã dựng tham chiếu", flush=True)


def main():
    """Cùng dữ liệu nến (CSV) đưa vào hàm production — cô lập đúng câu hỏi "công thức có khớp không"."""
    from datetime import timedelta

    from goldpulse.services.calendar import is_market_open

    raw_bars = [
        {"ts": r.ts.to_pydatetime(), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
        for r in raw.sort_values("ts").itertuples()
    ]
    ts_list = [b["ts"] for b in raw_bars]
    import bisect

    worst, flips, fdiff = 0.0, 0, {}
    for i in picks:
        t = df["ts"].iloc[i].to_pydatetime()
        hi = bisect.bisect_right(ts_list, t)
        lo = bisect.bisect_left(ts_list, t - model15m.LOOKBACK + timedelta(minutes=15))
        bars = [b for b in raw_bars[lo:hi] if is_market_open(b["ts"])]
        f = model15m.features(bars)
        if f is None:
            print("thiếu lịch sử tại", t)
            continue
        r = model15m.predict(f)
        worst = max(worst, abs(r["p_up"] - P[i]))
        flips += r["trigger"] != (abs(P[i] - 0.5) >= md["confidence_threshold"])
        for k, name in enumerate(FEATURES):
            fdiff[name] = max(fdiff.get(name, 0.0), abs(f[name] - FV[i][k]))
    print(f"{len(picks)} nến (gồm {min(NPICK // 3, len(trig))} nến trigger): lệch xác suất lớn nhất {worst:.5f} · lệch trigger {flips}")
    print("lệch đặc trưng lớn nhất:", {k: round(v, 5) for k, v in sorted(fdiff.items(), key=lambda t: -t[1])[:8]})


main()
