"""Tua NHIỀU họ chiến thuật BTC khung 1 giờ trên đúng luật tài khoản giấy vòng 4, chọn ra các luật khác biệt nhất.

Luật tài khoản (chủ dự án chốt 2026-10-03): 10 USD mỗi ngày, reset 00:00 UTC · đòn bẩy x20 (lệnh = 20 × số dư)
· phí 0,05% + trượt 0,02% mỗi chiều (0,14% khứ hồi trên giá trị lệnh) · mỗi tài khoản một lệnh một lúc
· cháy khi lỗ tạm (nến 15m) + phí ≥ số dư → mất số dư, nghỉ tới hết ngày · lệnh còn mở lúc 23:45 UTC đóng ở nến đó.

Chống khớp quá mức: mỗi họ luyện/ước lượng trên 2018–21, chọn biến thể trên 2022–24 ("chọn"),
chỉ báo kết quả 2025–26 ("KIỂM") sau khi đã chọn. Tương quan lãi/lỗ theo ngày đo trên 2025–26.

    python -m uv run --no-project --with numpy --with pandas --with scikit-learn python research/btc_strategies.py
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).parent))
from btc_methods import features, resample  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
FEE_SIDE = 0.0007
LEV = 20
START_BAL = 10.0
SRC = Path(__file__).with_name("btc_bars15.csv")


@dataclass
class Variant:
    family: str
    name: str
    sig: np.ndarray  # theo nến 1h: −1 / 0 / +1
    hold: int  # số giờ giữ tối đa
    tp: float | None = None  # bội số σ1h
    sl: float | None = None
    res: dict = field(default_factory=dict)


def load():
    b15 = pd.read_csv(SRC, parse_dates=["ts"]).sort_values("ts").reset_index(drop=True)
    h1 = resample(b15, "1h")
    pos = pd.Series(np.arange(len(b15)), index=b15["ts"])
    close_ts = h1["ts"] + pd.Timedelta(minutes=45)
    e15 = pos.reindex(close_ts).values  # nến 15m chứa giá đóng của nến 1h
    day = b15["ts"].dt.floor("D")
    last_of_day = b15.groupby(day).cumcount(ascending=False).values == 0
    # chỉ số nến 15m cuối cùng của ngày UTC cho mỗi nến 15m
    idx = np.arange(len(b15))
    end_idx = pd.Series(np.where(last_of_day, idx, np.nan)).bfill().values.astype(int)
    return b15, h1, e15, end_idx


def simulate(v: Variant, b15, h1, e15, end_idx, sigma) -> pd.DataFrame:
    o, h, lo, c = (b15[k].values for k in ("open", "high", "low", "close"))
    days = b15["ts"].dt.floor("D").values
    rows = []
    cur_day, bal, burned, busy = None, START_BAL, False, -1
    for j in np.flatnonzero(v.sig != 0):
        e = e15[j]
        if np.isnan(e):
            continue
        e = int(e)
        d = days[e]
        if d != cur_day:
            cur_day, bal, burned = d, START_BAL, False
        if burned or e < busy or bal <= 0:
            continue
        dr = v.sig[j]
        px = c[e]
        exit_i = min(e + 4 * v.hold, end_idx[e])
        if exit_i <= e:
            continue
        notional = LEV * bal
        burn_frac = (bal - notional * FEE_SIDE * 2) / notional
        tp = v.tp * sigma[j] if v.tp else None
        sl = v.sl * sigma[j] if v.sl else None
        gross, how = None, "time"
        for k in range(e + 1, exit_i + 1):
            adverse = (px - lo[k]) / px if dr == 1 else (h[k] - px) / px
            favour = (h[k] - px) / px if dr == 1 else (px - lo[k]) / px
            if adverse >= burn_frac and (sl is None or sl >= burn_frac):
                gross, how = -burn_frac, "burn"
                break
            if sl is not None and adverse >= sl:
                gross, how = -sl, "sl"
                break
            if tp is not None and favour >= tp:
                gross, how = tp, "tp"
                break
        if gross is None:
            exit_i_used = exit_i
            gross = dr * (c[exit_i_used] / px - 1)
        else:
            exit_i_used = k
        pnl = max(notional * (gross - 2 * FEE_SIDE), -bal)
        bal += pnl
        if bal <= 1e-9 or how == "burn":
            burned, bal = True, 0.0
        busy = exit_i_used
        rows.append((d, gross, gross - 2 * FEE_SIDE, pnl, bal <= 0))
    return pd.DataFrame(rows, columns=["day", "gross", "net", "pnl", "burn"])


def summarize(t: pd.DataFrame, lo_y: int, hi_y: int, ndays: int) -> dict:
    yr = pd.DatetimeIndex(t["day"]).year
    s = t[(yr >= lo_y) & (yr <= hi_y)]
    if s.empty:
        return {
            "n": 0,
            "per4w": 0,
            "hit": np.nan,
            "gross": np.nan,
            "net": np.nan,
            "usd4w": 0,
            "burn": 0,
            "winday": np.nan,
        }
    daily = s.groupby("day")["pnl"].sum()
    return {
        "n": len(s), "per4w": len(s) / ndays * 28, "hit": (s.gross > 0).mean(), "gross": s.gross.mean() * 100,
        "net": s.net.mean() * 100, "usd4w": s.pnl.sum() / ndays * 28,
        "burn": s.groupby("day")["burn"].any().sum() / ndays, "winday": (daily > 0).mean(),
    }  # fmt: skip


def build_variants(h1: pd.DataFrame) -> tuple[list[Variant], np.ndarray]:
    f = features(h1, 24)
    c = h1["close"]
    yr = h1["ts"].dt.year.values
    lr = np.log(c).diff()
    sigma = lr.rolling(24).std().values
    r1 = lr.values
    ok = f.notna().all(axis=1).values
    tr = ok & (yr <= 2021)
    sel = ok & (yr >= 2022) & (yr <= 2024)
    V: list[Variant] = []

    # 1. Mô hình logistic (như vàng) — lọc theo độ tự tin
    X = f.fillna(0).values
    sc = StandardScaler().fit(X[tr])
    fwd1 = np.log(c.shift(-1) / c).values
    lg = LogisticRegression(C=0.1, max_iter=2000).fit(sc.transform(X[tr]), (fwd1[tr] > 0).astype(int))
    p = lg.predict_proba(sc.transform(X))[:, 1]
    conf = np.where(ok, np.abs(p - 0.5), 0)
    dm = np.where(p > 0.5, 1, -1)
    trend = np.sign(f["ema_gap"].fillna(0).values)
    for q in (0.02, 0.05, 0.10):
        thr = np.quantile(conf[sel], 1 - q)
        base = np.where(conf >= thr, dm, 0)
        for H in (1, 4):
            V.append(Variant("mô hình", f"top {q:.0%} giữ {H}h", base, H))
        V.append(
            Variant("mô hình + xu hướng", f"top {q:.0%} cùng chiều EMA 24h, giữ 1h", np.where(dm == trend, base, 0), 1)
        )
        V.append(Variant("mô hình + chốt/cắt", f"top {q:.0%} chốt 1σ cắt 1σ, tối đa 4h", base, 4, tp=1.0, sl=1.0))
    V.append(Variant("đối chứng", "ngược mô hình top 5%", -np.where(conf >= np.quantile(conf[sel], 0.95), dm, 0), 1))

    # 2. Nến giật mạnh: đánh ngược (hồi) hoặc đánh theo (đà)
    z = np.nan_to_num(r1 / np.roll(sigma, 1))
    for k in (2.0, 3.0):
        for H in (1, 4):
            V.append(
                Variant("đánh ngược nến giật", f"|Δ1h| ≥ {k}σ, giữ {H}h", np.where(np.abs(z) >= k, -np.sign(z), 0), H)
            )
            V.append(
                Variant("đánh theo nến giật", f"|Δ1h| ≥ {k}σ, giữ {H}h", np.where(np.abs(z) >= k, np.sign(z), 0), H)
            )

    # 3. RSI(2) cực trị thuận xu hướng dài (kiểu Connors)
    rsi2 = f["rsi9"].values * 0  # placeholder để giữ shape
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    l_ = (-d).clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = (100 - 100 / (1 + g / l_)).values
    ema200 = c.ewm(span=200).mean().values
    for lo_, H in ((10, 2), (10, 4), (5, 4)):
        s = np.where((rsi2 < lo_) & (c.values > ema200), 1, np.where((rsi2 > 100 - lo_) & (c.values < ema200), -1, 0))
        V.append(Variant("RSI2 hồi thuận xu hướng", f"RSI2 < {lo_}/> {100 - lo_} theo EMA200h, giữ {H}h", s, H))

    # 4. Phá đỉnh/đáy N giờ (Donchian)
    for N in (24, 48):
        hi = h1["high"].rolling(N).max().shift(1).values
        lw = h1["low"].rolling(N).min().shift(1).values
        s = np.where(c.values > hi, 1, np.where(c.values < lw, -1, 0))
        for H in (4, 8):
            V.append(Variant("phá đỉnh/đáy", f"{N}h, giữ {H}h", s, H))

    # 5. Nén biên độ rồi bung (Bollinger squeeze)
    m20, s20 = c.rolling(20).mean(), c.rolling(20).std()
    width = 4 * s20 / m20
    narrow = width < width.rolling(720).quantile(0.2)
    s = np.where(narrow.shift(1, fill_value=False) & (c > m20 + 2 * s20), 1,
                 np.where(narrow.shift(1, fill_value=False) & (c < m20 - 2 * s20), -1, 0))  # fmt: skip
    for H in (4, 8):
        V.append(Variant("nén rồi bung", f"Bollinger hẹp 20% thấp nhất 30 ngày, giữ {H}h", s, H))

    # 6. Giờ trong ngày — ước lượng trên 2018–21, chọn giờ có |t| lớn nhất
    hr = h1["ts"].dt.hour.values
    t_tr = pd.DataFrame({"hr": hr[tr], "r": fwd1[tr]}).groupby("hr")["r"].agg(["mean", "std", "count"])
    t_tr["t"] = t_tr["mean"] / (t_tr["std"] / np.sqrt(t_tr["count"]))
    for k in (2, 4):
        best = t_tr["t"].abs().nlargest(k).index
        dirs = np.sign(t_tr.loc[best, "mean"])
        s = np.zeros(len(c), dtype=int)
        for hh, dd in dirs.items():
            s[(hr == hh)] = int(dd)
        V.append(
            Variant(
                "giờ trong ngày",
                f"{k} giờ mạnh nhất 2018–21 ({', '.join(f'{x}h{"+" if dirs[x] > 0 else "−"}' for x in best)}), giữ 1h",
                s,
                1,
            )
        )

    # 7. Khối lượng đột biến — theo/ngược chiều thân nến
    vr = np.nan_to_num(f["vratio"].values)
    body = np.sign(np.nan_to_num(f["body"].values))
    for H in (1, 4):
        V.append(
            Variant("khối lượng đột biến theo", f"KL ≥ 3× TB 24h, giữ {H}h", np.where(vr >= np.log(3), body, 0), H)
        )
        V.append(
            Variant("khối lượng đột biến ngược", f"KL ≥ 3× TB 24h, giữ {H}h", np.where(vr >= np.log(3), -body, 0), H)
        )

    # 8. EMA 9/21 cắt nhau
    e9, e21 = c.ewm(span=9).mean(), c.ewm(span=21).mean()
    up = (e9 > e21).astype(int)
    cross = up.diff().fillna(0).values
    for H in (4, 8):
        V.append(Variant("EMA 9/21 cắt", f"giữ {H}h", np.sign(cross).astype(int), H))

    # 9. Lệch xa trung bình ngày → về trung bình
    gap = f["ema_gap"].values / np.nan_to_num(sigma, nan=1) / np.sqrt(24)
    for k in (1.5, 2.5):
        V.append(
            Variant(
                "về trung bình ngày",
                f"lệch EMA24h ≥ {k}σ ngày, giữ 4h",
                np.where(np.abs(gap) >= k, -np.sign(gap), 0),
                4,
            )
        )

    for v in V:
        v.sig = np.where(ok, np.nan_to_num(v.sig), 0).astype(int)
    return V, sigma


def main() -> None:
    b15, h1, e15, end_idx = load()
    V, sigma = build_variants(h1)
    nd_sel = (pd.Timestamp("2025-01-01") - pd.Timestamp("2022-01-01")).days
    nd_te = (b15["ts"].iloc[-1].tz_localize(None) - pd.Timestamp("2025-01-01")).days
    trades = {}
    for v in V:
        t = simulate(v, b15, h1, e15, end_idx, sigma)
        v.res["sel"] = summarize(t, 2022, 2024, nd_sel)
        v.res["te"] = summarize(t, 2025, 2026, nd_te)
        trades[v.family + " · " + v.name] = t

    def line(r):
        return (f"{r['n']:>5} lệnh ({r['per4w']:4.0f}/4t) · đúng {r['hit']:5.1%} · trước phí {r['gross']:+.3f}% · "
                f"sau phí {r['net']:+.3f}% · {r['usd4w']:+7.2f} USD/4 tuần · cháy {r['burn']:5.1%} ngày · ngày lãi {r['winday']:5.1%}")  # fmt: skip

    print("TẤT CẢ BIẾN THỂ — chọn (2022–24) | KIỂM (2025–26)\n")
    for v in V:
        print(f"[{v.family}] {v.name}\n   chọn: {line(v.res['sel'])}\n   KIỂM: {line(v.res['te'])}")

    # Mỗi họ: biến thể tốt nhất theo 'chọn' (sau phí %/lệnh, đủ ≥ 10 lệnh/4 tuần), rồi mới nhìn KIỂM
    print("\nĐẠI DIỆN MỖI HỌ (chọn bằng 2022–24, không nhìn KIỂM):")
    reps = {}
    for fam in dict.fromkeys(v.family for v in V):
        cand = [v for v in V if v.family == fam and v.res["sel"]["per4w"] >= 10]
        if not cand:
            continue
        best = max(cand, key=lambda v: v.res["sel"]["net"])
        reps[fam] = best
        print(f"  {fam:28} {best.name}\n     chọn: {line(best.res['sel'])}\n     KIỂM: {line(best.res['te'])}")

    # Tương quan lãi/lỗ theo ngày trên 2025–26 giữa các đại diện
    days = pd.date_range("2025-01-01", b15["ts"].iloc[-1].tz_localize(None).floor("D"), freq="D")
    picks = {  # A/B/C chủ dự án chọn trước (2026-10-03) + đại diện từng họ
        "A": "mô hình · top 5% giữ 1h", "B": "mô hình · top 5% giữ 4h", "C": "mô hình · top 2% giữ 1h",
    }  # fmt: skip
    for i, (fam, v) in enumerate(reps.items()):
        picks[f"h{i + 1}"] = v.family + " · " + v.name
    M = {}
    for code, key in picks.items():
        t = trades[key]
        dd = t.assign(day=pd.to_datetime(t["day"]).dt.tz_localize(None)).groupby("day")["pnl"].sum()
        M[code] = dd.reindex(days, fill_value=0.0)
    print("\nMã:", " · ".join(f"{k} = {v}" for k, v in picks.items()))
    corr = pd.DataFrame(M).corr().round(2)
    pd.set_option("display.width", 250)
    print("\nTương quan lãi/lỗ theo ngày 2025–26 (gần 0 = khác biệt; âm = bù trừ nhau):")
    print(corr.to_string())


if __name__ == "__main__":
    main()
