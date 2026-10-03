"""(v4) Mô hình `btc_logit1h_v1` + 4 chiến thuật BTC khung 1h — đúng `research/btc_strategies.py` (ARCHITECTURE §6d).

A `model`: |p − 0,5| ≥ thr_top5, hướng mô hình · B `model_trend`: ≥ thr_top10 và cùng dấu với (close − EMA 24h)
C `spike`: |Δlog close 1h| ≥ 3 × σ24 của nến trước, theo nến · D `squeeze`: độ rộng Bollinger nến trước < phân vị 20%
của 720 nến, close ngoài dải → theo phía phá. Mọi chiến thuật chỉ bật khi đủ mọi đặc trưng (như `ok` bên nghiên cứu).
"""

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import features as feat

MODEL_FILE = Path(__file__).resolve().parents[4] / "data" / "models" / "btc_logit1h_v1.json"
SQUEEZE_WINDOW = 720
SQUEEZE_Q = 0.2
# Đủ cho cửa sổ dài nhất (480 cho chế độ biến động, 720 + 20 cho nén) — ARCHITECTURE §6d
MIN_BARS = 760


@lru_cache(maxsize=1)
def model() -> dict[str, Any]:
    return json.loads(MODEL_FILE.read_text(encoding="utf-8"))


def p_up(f: dict[str, Any], md: dict[str, Any]) -> float | None:
    xs = [f.get(k) for k in md["features"]]
    if any(x is None for x in xs):
        return None
    z = md["intercept"] + math.fsum(
        c * (x - m) / s for c, x, m, s in zip(md["coef"], xs, md["mean"], md["scale"], strict=True)
    )
    return 1 / (1 + math.exp(-z))


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def evaluate(bars: list[dict[str, Any]], only_last: bool = False) -> list[dict[str, Any]]:
    """Mỗi nến 1h → {bar_ts, close, values, fired{kind: −1/0/1}}. `only_last` = chỉ trả hàng cuối (worker)."""
    md = model()
    fs = feat.compute(bars)
    widths = [f["_width"] for f in fs]
    q20 = feat.rolling(widths, SQUEEZE_WINDOW, lambda w: feat.quantile(w, SQUEEZE_Q))
    out = []
    rng = range(len(bars) - 1, len(bars)) if only_last else range(len(bars))
    for i in rng:
        f = fs[i]
        ok = all(f.get(k) is not None for k in md["features"])
        p = p_up(f, md) if ok else None
        conf = abs(p - 0.5) if p is not None else None
        dm = 1 if (p is not None and p > 0.5) else -1
        prev_sigma = fs[i - 1]["_sigma"] if i >= 1 else None
        z = f["_r1"] / prev_sigma if (f["_r1"] is not None and prev_sigma) else 0.0
        if not math.isfinite(z):
            z = 0.0
        prev_w = widths[i - 1] if i >= 1 else None
        prev_q = q20[i - 1] if i >= 1 else None
        narrow = prev_w is not None and prev_q is not None and prev_w < prev_q
        c = float(bars[i]["close"])
        m20, s20 = f["_m20"], f["_s20"]
        upper = m20 + 2 * s20 if m20 is not None and s20 is not None else None
        lower = m20 - 2 * s20 if m20 is not None and s20 is not None else None
        fired = {"model": 0, "model_trend": 0, "spike": 0, "squeeze": 0}
        if ok:
            if conf is not None and conf >= md["thr_top5"]:
                fired["model"] = dm
            if conf is not None and conf >= md["thr_top10"] and dm == _sign(f["ema_gap"]):
                fired["model_trend"] = dm
            if abs(z) >= 3.0:
                fired["spike"] = _sign(z)
            if narrow and upper is not None and lower is not None:
                fired["squeeze"] = 1 if c > upper else -1 if c < lower else 0
        out.append(
            {
                "bar_ts": bars[i]["bar_ts"],
                "close": c,
                "values": {
                    "ok": ok,
                    "p_up": p,
                    "conf": conf,
                    "ema_gap": f["ema_gap"],
                    "z1h": z,
                    "sigma": prev_sigma,
                    "bb_width_prev": prev_w,
                    "bb_q20_prev": prev_q,
                    "bb_upper": upper,
                    "bb_lower": lower,
                },
                "fired": fired,
            }
        )
    return out
