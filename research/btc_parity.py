"""Đối chiếu bản app (`services/btc/strategies.py`, Python thuần, chỉ thấy 90 ngày nến như worker) với bản nghiên cứu
(`btc_strategies.py`, pandas, toàn bộ lịch sử) — từng giờ, 4 chiến thuật, N ngày cuối (AC-17, H2 EXPERIMENTS-v4).

    PYTHONPATH=src python -m uv run --no-project --with numpy --with pandas --with scikit-learn python research/btc_parity.py [ngày=30]
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from btc_methods import features, resample  # noqa: E402
from btc_strategies import build_variants  # noqa: E402

from goldpulse.services.btc import strategies as app  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
PICK = {
    "model": ("mô hình", "top 5% giữ 1h"),
    "model_trend": ("mô hình + xu hướng", "top 10% cùng chiều EMA 24h, giữ 1h"),
    "spike": ("đánh theo nến giật", "|Δ1h| ≥ 3.0σ, giữ 4h"),
    "squeeze": ("nén rồi bung", "Bollinger hẹp 20% thấp nhất 30 ngày, giữ 4h"),
}


def main(days: int = 30) -> int:
    b15 = pd.read_csv(Path(__file__).with_name("btc_bars15.csv"), parse_dates=["ts"])
    h1 = resample(b15, "1h")
    V, _ = build_variants(h1)
    ref = {k: next(v.sig for v in V if (v.family, v.name) == fam) for k, fam in PICK.items()}
    end = h1["ts"].iloc[-1]
    win = h1[h1["ts"] > end - pd.Timedelta(days=90)].reset_index()
    bars = [
        {
            "bar_ts": r.ts.to_pydatetime(),
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in win.itertuples()
    ]
    got = app.evaluate(bars)
    first = end - pd.Timedelta(days=days)
    bad = 0
    n = 0
    fired = {k: 0 for k in PICK}
    f_ref = features(h1, 24)
    for j, g in zip(win["index"], got, strict=True):
        if h1["ts"].iloc[j] <= first:
            continue
        n += 1
        for k in PICK:
            want = int(ref[k][j])
            fired[k] += want != 0
            if g["fired"][k] != want:
                bad += 1
                print(
                    f"LỆCH {h1['ts'].iloc[j]} {k}: app {g['fired'][k]} · nghiên cứu {want} · app values {g['values']}"
                )
        if g["values"]["ok"] and abs(g["values"]["ema_gap"] - f_ref["ema_gap"].iloc[j]) > 1e-9:
            print(f"ema_gap lệch {h1['ts'].iloc[j]}: {g['values']['ema_gap']} vs {f_ref['ema_gap'].iloc[j]}")
    print(f"{n} giờ · {days} ngày cuối · tín hiệu nghiên cứu: {fired} · lệch {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
