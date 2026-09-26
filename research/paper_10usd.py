"""Giao dịch giấy tài khoản 10 USD/ngày theo dự đoán tự động (logit15m_v1) — chọn luật trên 2022–2024, kiểm 2025–2026.

    python -m uv run --no-project --with numpy --with pandas python research/paper_10usd.py research/bars15.csv

Luật lệnh: vào ở close nến trigger theo hướng mô hình; ra ở close nến kế tiếp (15 phút) hoặc chạm cắt lỗ
trong nến kế tiếp (dùng high/low nến 15 phút — cắt lỗ chỉ một phía nên thứ tự chạm không quan trọng).
Chi phí mỗi lệnh = spread 0,30 USD/oz (một lần, khứ hồi). Mỗi ngày giao dịch (18:00 NY → 17:00 NY) bắt đầu lại 10 USD;
không đủ ký quỹ thì hết lệnh trong ngày. Nến kế tiếp không liền kề (thị trường đóng) → không vào lệnh (như production).
"""

import json
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from features import build

sys.stdout.reconfigure(encoding="utf-8")
NY = ZoneInfo("America/New_York")
START, SPREAD = 10.0, 0.30

raw = pd.read_csv(sys.argv[1], parse_dates=["ts"])
df, F = build(raw)
md = json.load(open(__import__("pathlib").Path(__file__).resolve().parent.parent / "data/models/logit15m_v1.json", encoding="utf-8"))
FV = F.values
Z = (FV - np.array(md["mean"])) / np.array(md["scale"])
P = 1 / (1 + np.exp(-(Z @ np.array(md["coef"]) + md["intercept"])))
ok = np.isfinite(FV).all(axis=1)
trig = ok & (np.abs(P - 0.5) >= md["confidence_threshold"])

ts = df["ts"]
gap_next = (ts.shift(-1) - ts).dt.total_seconds().values
c, h, lo = df["close"].values, df["high"].values, df["low"].values
# ngày giao dịch: 18:00 NY hôm trước → 17:00 NY hôm nay
tday = (ts.dt.tz_convert(NY) + pd.Timedelta(hours=6)).dt.date.values
yr = ts.dt.year.values

idx = [i for i in np.where(trig)[0] if i + 1 < len(c) and gap_next[i] == 900]
T = pd.DataFrame({"i": idx})
T["dir"] = np.where(P[idx] > 0.5, 1, -1)
T["entry"] = c[idx]
T["exit"] = c[[i + 1 for i in idx]]
T["adverse"] = np.where(T["dir"] == 1, T["entry"] - lo[[i + 1 for i in idx]], h[[i + 1 for i in idx]] - T["entry"])
T["day"] = tday[idx]
T["yr"] = yr[idx]
print(f"lệnh ứng viên: {len(T)} · giá gần nhất {c[-1]:.0f} USD/oz")


def run(T, oz, sl, lev, max_loss_day=None, max_trades=None):
    """Trả bảng kết quả theo ngày. sl=None: chỉ thoát sau 15 phút."""
    rows = []
    for day, g in T.groupby("day", sort=True):
        eq, n, wins = START, 0, 0
        for r in g.itertuples():
            if max_trades and n >= max_trades:
                break
            if max_loss_day is not None and eq <= START - max_loss_day:
                break
            if eq < oz * r.entry / lev:  # không đủ ký quỹ
                break
            move = r.dir * (r.exit - r.entry)
            if sl is not None and r.adverse >= sl:
                move = -sl
            pnl = oz * (move - SPREAD)
            eq = max(eq + pnl, 0.0)
            n += 1
            wins += pnl > 0
        rows.append({"day": day, "end": eq, "n": n, "wins": wins})
    return pd.DataFrame(rows)


def show(label, D):
    pnl = D["end"] - START
    n = D["n"].sum()
    print(f"{label:<44} ngày {len(D):>3} · lệnh {n:>4} · thắng {D['wins'].sum() / max(n, 1):5.1%} · "
          f"lãi TB/ngày {pnl.mean():+6.2f} · ngày lãi {(pnl > 0).mean():5.1%} · cháy {(D['end'] < 1).mean():5.1%} · "
          f"tệ nhất {pnl.min():+6.2f} · tổng {pnl.sum():+8.2f}")


B, C = T[T["yr"].between(2022, 2024)], T[T["yr"] >= 2025]
print("\n== Không phí, không cắt lỗ — kiểm lại tỉ lệ đúng hướng ==")
for name, S in (("2022–24", B), ("2025–26", C)):
    print(f"{name}: {len(S)} lệnh · đúng hướng {(S['dir'] * (S['exit'] - S['entry']) > 0).mean():.1%} · "
          f"Δ TB theo hướng {(S['dir'] * (S['exit'] - S['entry'])).mean():+.3f} USD/oz · "
          f"|Δ| TB {(S['exit'] - S['entry']).abs().mean():.3f} · spread {SPREAD}")

grid = []
for oz in (1.0, 0.01):
    for sl in (None, 1, 2, 3, 5, 8):
        for mld in (None, 3, 5):
            grid.append((oz, sl, mld))

print("\n== CHỌN LUẬT trên 2022–2024 (đòn bẩy 1:1000; 1 oz = 0,01 lot tài khoản thường, 0,01 oz = 0,01 lot tài khoản cent) ==")
res = []
for oz, sl, mld in grid:
    D = run(B, oz, sl, 1000, mld)
    label = f"{oz:g} oz · cắt lỗ {sl or '—'} · dừng ngày khi lỗ {mld or '—'}"
    show(label, D)
    res.append(((D["end"] - START).sum(), oz, sl, mld))

best = max(r for r in res if r[1] == 1.0)
print(f"\nTốt nhất trên 2022–24 (1 oz): cắt lỗ {best[2]} · dừng ngày {best[3]} · tổng {best[0]:+.2f}")
print("\n== KIỂM 2025–2026 (nhìn một lần) ==")
for oz, sl, mld in ((1.0, best[2], best[3]), (1.0, None, None), (0.01, best[2], best[3])):
    show(f"{oz:g} oz · cắt lỗ {sl or '—'} · dừng ngày khi lỗ {mld or '—'}", run(C, oz, sl, 1000, mld))
print("\n== Ký quỹ 1 oz theo đòn bẩy (giá gần nhất) ==")
for lev in (100, 200, 500, 1000, 2000):
    print(f"1:{lev}: {c[-1] / lev:.2f} USD")
