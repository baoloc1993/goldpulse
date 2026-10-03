"""Xuất mô hình BTC `btc_logit1h_v1` ra JSON cho app — ĐÚNG mô hình đã tua ở `btc_strategies.py` (DECISIONS 2026-10-03:
đóng băng, không luyện lại). Luyện 2018–21 · ngưỡng tự tin top 5% / top 10% lấy trên 2022–24.

    python -m uv run --no-project --with numpy --with pandas --with scikit-learn python research/btc_export_model.py
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).parent))
from btc_methods import features, resample  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(__file__).resolve().parents[1] / "data" / "models" / "btc_logit1h_v1.json"


def main() -> None:
    b15 = pd.read_csv(Path(__file__).with_name("btc_bars15.csv"), parse_dates=["ts"])
    h1 = resample(b15, "1h")
    f = features(h1, 24)
    c = h1["close"]
    yr = h1["ts"].dt.year.values
    ok = f.notna().all(axis=1).values
    tr = ok & (yr <= 2021)
    sel = ok & (yr >= 2022) & (yr <= 2024)
    X = f.fillna(0).values
    sc = StandardScaler().fit(X[tr])
    fwd1 = np.log(c.shift(-1) / c).values
    lg = LogisticRegression(C=0.1, max_iter=2000).fit(sc.transform(X[tr]), (fwd1[tr] > 0).astype(int))
    p = lg.predict_proba(sc.transform(X))[:, 1]
    conf = np.where(ok, np.abs(p - 0.5), 0)
    model = {
        "name": "btc_logit1h_v1",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "features": list(f.columns),
        "mean": sc.mean_.tolist(),
        "scale": sc.scale_.tolist(),
        "coef": lg.coef_[0].tolist(),
        "intercept": float(lg.intercept_[0]),
        "thr_top5": float(np.quantile(conf[sel], 0.95)),
        "thr_top10": float(np.quantile(conf[sel], 0.90)),
        "trained": "2018-01-01..2021-12-31 (Binance spot 1m → 1h), C=0.1",
        "thresholds_from": "2022-01-01..2024-12-31",
        "tested": "2025-01-01..2026-10-02 — EXPERIMENTS-v4.md",
        "source": "research/btc_export_model.py",
    }
    OUT.write_text(json.dumps(model, indent=1), encoding="utf-8")
    print(
        OUT,
        "·",
        len(model["features"]),
        "đặc trưng · thr_top5",
        round(model["thr_top5"], 5),
        "· thr_top10",
        round(model["thr_top10"], 5),
    )


if __name__ == "__main__":
    main()
