"""Tua lịch sử BTC — chọn khung đoán (15m / 1h / 4h) và luật vào lệnh cho vòng 4.

Cùng phương pháp vòng 2–3 của vàng: luyện 2018–2021 · chọn ngưỡng 2022–2024 · kiểm cuối 2025–2026
(không nhìn kiểm cuối khi chọn). Phí khứ hồi COST = 2 × (0,05% phí + 0,02% trượt) = 0,14% giá trị lệnh.
Lệnh: vào ở close nến trigger, ra ở close nến kế (giữ 1 nến) — hoặc giữ H nến với luật "giữ lâu".

    python -m uv run --no-project --with numpy --with pandas --with scikit-learn python research/btc_methods.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.stdout.reconfigure(encoding="utf-8")
COST = 0.0014
SRC = Path(__file__).with_name("btc_bars15.csv")
TF = {"15m": "15min", "1h": "1h", "4h": "4h"}
DAY = {"15m": 96, "1h": 24, "4h": 6}


def resample(b15: pd.DataFrame, rule: str) -> pd.DataFrame:
    g = b15.set_index("ts").resample(rule)
    b = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last(), "volume": g["volume"].sum()})  # fmt: skip
    return b.dropna().reset_index()


def rsi(x: pd.Series, n: int) -> pd.Series:
    d = x.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l_ = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l_)


def features(b: pd.DataFrame, day: int) -> pd.DataFrame:
    c, o, h, lo, v = (b[k] for k in ("close", "open", "high", "low", "volume"))
    lr = np.log(c).diff()
    f = pd.DataFrame(index=b.index)
    for k in (1, 2, 4, 8, 16, 32):
        f[f"r{k}"] = np.log(c / c.shift(k))
    f["rday"] = np.log(c / c.shift(day))
    vol = lr.rolling(day).std()
    f["vol"] = vol
    f["vol_regime"] = vol / lr.rolling(day * 20).std()
    m20, s20 = c.rolling(20).mean(), c.rolling(20).std()
    f["pctb"] = (c - (m20 - 2 * s20)) / (4 * s20)
    f["rsi9"], f["rsi14"] = rsi(c, 9), rsi(c, 14)
    rng = (h - lo).replace(0, np.nan)
    f["body"] = (c - o) / rng
    f["upwick"] = (h - np.maximum(c, o)) / rng
    f["lowick"] = (np.minimum(c, o) - lo) / rng
    f["range_vol"] = np.log(h / lo) / vol
    f["vratio"] = np.log(v / v.rolling(day).mean())
    f["d_hi"] = np.log(c / h.rolling(day).max())
    f["d_lo"] = np.log(c / lo.rolling(day).min())
    f["ema_gap"] = np.log(c / c.ewm(span=day).mean())
    hr = b["ts"].dt.hour
    f["hsin"], f["hcos"] = np.sin(2 * np.pi * hr / 24), np.cos(2 * np.pi * hr / 24)
    f["wkend"] = (b["ts"].dt.weekday >= 5).astype(float)
    return f.replace([np.inf, -np.inf], np.nan)  # nến volume 0 → log(0)


def stats(net: np.ndarray, days: float) -> str:
    if len(net) == 0:
        return "   0 lệnh"
    per4w = len(net) / days * 28
    return (f"{len(net):>6} lệnh ({per4w:5.0f}/4 tuần) · đúng hướng {(net + COST > 0).mean():5.1%} · "
            f"trước phí {(net + COST).mean() * 100:+.3f}% · sau phí {net.mean() * 100:+.3f}%/lệnh")  # fmt: skip


def run(name: str, b: pd.DataFrame) -> None:
    day = DAY[name]
    f = features(b, day)
    c = b["close"].values
    fwd = {H: np.log(np.roll(c, -H) / c) for H in (1, 4)}
    yr = b["ts"].dt.year.values
    ok = f.notna().all(axis=1).values & (np.arange(len(c)) < len(c) - 4)
    tr, sel, te = ok & (yr <= 2021), ok & (yr >= 2022) & (yr <= 2024), ok & (yr >= 2025)
    span = {k: (b["ts"][m].iloc[-1] - b["ts"][m].iloc[0]).days for k, m in (("sel", sel), ("te", te))}
    X = f.values
    sc = StandardScaler().fit(X[tr])
    y = (fwd[1] > 0).astype(int)
    lg = LogisticRegression(C=0.1, max_iter=2000).fit(sc.transform(X[tr]), y[tr])
    p = lg.predict_proba(sc.transform(np.nan_to_num(X)))[:, 1]
    conf = np.abs(p - 0.5)
    d = np.where(p > 0.5, 1, -1)
    print(f"\n=== {name} · {len(c)} nến · biên độ TB |Δ1 nến| {np.nanmean(np.abs(fwd[1][ok])) * 100:.3f}% "
          f"(phí 0,14% = {COST / np.nanmean(np.abs(fwd[1][ok])):.0%} biên độ) ===")  # fmt: skip
    print(f"logistic, đúng hướng mọi nến: chọn {((p[sel] > 0.5) == y[sel]).mean():.1%} · kiểm {((p[te] > 0.5) == y[te]).mean():.1%}")
    for H in (1, 4):
        print(f"-- mô hình, giữ {H} nến · top q tự tin (ngưỡng lấy từ 2022–24)")
        for q in (0.02, 0.05, 0.10, 0.20, 0.50, 1.0):
            thr = np.quantile(conf[sel], 1 - q)
            for lbl, m in (("chọn", sel), ("KIỂM", te)):
                k = m & (conf >= thr)
                print(f"  top {q:4.0%} {lbl}: {stats(d[k] * fwd[H][k] - COST, span['sel' if lbl == 'chọn' else 'te'])}")
    print("-- luật đơn giản (không luyện), giữ 1 nến")
    rday = f["rday"].values
    rules = {
        "theo đà ngày (dấu rday)": np.sign(rday),
        "ngược đà ngày": -np.sign(rday),
        "phá đỉnh/đáy 1 ngày": np.where(f["d_hi"].values >= 0, 1, np.where(f["d_lo"].values <= 0, -1, 0)),
        "rsi14 quá bán/quá mua đảo": np.where(f["rsi14"].values < 30, 1, np.where(f["rsi14"].values > 70, -1, 0)),
    }
    for rn, sig in rules.items():
        for lbl, m in (("chọn", sel), ("KIỂM", te)):
            k = m & (sig != 0)
            print(f"  {rn:28} {lbl}: {stats(sig[k] * fwd[1][k] - COST, span['sel' if lbl == 'chọn' else 'te'])}")
    # Theo năm cho top 10% mô hình giữ 1 nến — xem có ổn định không
    thr = np.quantile(conf[sel], 0.9)
    row = []
    for yv in range(2022, 2027):
        k = ok & (yr == yv) & (conf >= thr)
        row.append(f"{yv}: {(d[k] * fwd[1][k] - COST).mean() * 100:+.3f}% ({k.sum()})")
    print("  top 10% theo năm (sau phí):", " · ".join(row))


def main() -> None:
    b15 = pd.read_csv(SRC, parse_dates=["ts"])
    for name, rule in TF.items():
        run(name, b15 if name == "15m" else resample(b15, rule))


if __name__ == "__main__":
    main()
