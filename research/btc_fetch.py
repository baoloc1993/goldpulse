"""Tải nến 1 phút BTCUSDT (Binance spot) từ data.binance.vision, gộp thành nến 15 phút.

Tháng đã khép: file zip theo tháng; tháng đang chạy: file zip theo ngày. Không key, không đăng nhập.
Ra `research/btc_bars15.csv` (ts UTC, open, high, low, close, volume, n1m) — không commit, tải lại được.

    python -m uv run --no-project --with pandas python research/btc_fetch.py
"""

import io
import sys
import urllib.request
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
BASE = "https://data.binance.vision/data/spot"
OUT = Path(__file__).with_name("btc_bars15.csv")
START = date(2018, 1, 1)
COLS = ["open_time", "open", "high", "low", "close", "volume"]


def fetch(url: str) -> pd.DataFrame | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = r.read()
    except Exception as e:  # tháng/ngày chưa có file → bỏ qua
        print("  bỏ qua", url.rsplit("/", 1)[-1], type(e).__name__)
        return None
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        df = pd.read_csv(z.open(z.namelist()[0]), header=None, usecols=range(6), names=COLS)
    t = df["open_time"]
    # Từ 2025 Binance ghi open_time bằng micro giây
    df["ts"] = pd.to_datetime(t, unit="us" if t.iloc[0] > 1e14 else "ms", utc=True)
    return df.drop(columns="open_time")


def to15(df: pd.DataFrame) -> pd.DataFrame:
    g = df.set_index("ts").resample("15min")
    b = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(), "n1m": g["close"].count(),
    })  # fmt: skip
    return b[b["n1m"] > 0].reset_index()


def main() -> None:
    today = datetime.now(UTC).date()
    parts = []

    def daily(d: date, end: date) -> None:
        while d < end:
            df = fetch(f"{BASE}/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{d:%Y-%m-%d}.zip")
            if df is not None:
                parts.append(to15(df))
            d += timedelta(days=1)

    m = START
    while (m.year, m.month) < (today.year, today.month):
        print("tháng", f"{m:%Y-%m}")
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        df = fetch(f"{BASE}/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{m:%Y-%m}.zip")
        if df is not None:
            parts.append(to15(df))
        else:  # tháng vừa khép, Binance chưa đóng gói theo tháng
            daily(m, nxt)
        m = nxt
    daily(today.replace(day=1), today)
    bars = pd.concat(parts).drop_duplicates("ts").sort_values("ts")
    bars.to_csv(OUT, index=False)
    print(f"{len(bars)} nến 15m · {bars.ts.iloc[0]} → {bars.ts.iloc[-1]} · thiếu phút: {(bars.n1m < 15).mean():.2%} nến")


if __name__ == "__main__":
    main()
