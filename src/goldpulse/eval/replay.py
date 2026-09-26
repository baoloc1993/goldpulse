"""Tua lại lịch sử để đo + luyện prompt dự đoán 15 phút (pha E vòng 2, DECISIONS 2026-09-26).

    python -m goldpulse.eval.replay sample --since 2025-01-01 --until 2026-09-20 --n 300 --out data/eval/samples.jsonl
    python -m goldpulse.eval.replay run --samples data/eval/samples.jsonl --split train --prompt <file.md> \
        --name v1 --budget 20
    python -m goldpulse.eval.replay report data/eval/run-*.jsonl
    python -m goldpulse.eval.replay day --date 2026-09-25 --days 5     # đi tới từng ngày, đúng luật production

Chống "biết trước": gói không có ngày, không có lịch FED, mức giá dời một khoảng ngẫu nhiên (RSI/%b/biên độ không đổi).
Chấm như production: close nến 15 phút kế tiếp so close nến có tín hiệu; |Δ| < 0,01 → loại.
Đường chuẩn miễn phí: đi theo hướng tín hiệu gợi ý (không gọi Claude) — Claude phải hơn đường này mới đáng tiền.
Chỉ ĐỌC kho (DATABASE_URL); không ghi prediction/bot_job — không đụng metric production.
"""

import argparse
import asyncio
import json
import math
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import SessionLocal, engine
from ..integrations.claude_base import BrainError
from ..integrations.claude_cli import ClaudeCli
from ..repositories import candles as candle_repo
from ..services import fed as fed_svc
from ..services import signals
from ..services.calendar import VN
from ..services.context_pack import WEEKDAYS

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "src/goldpulse/brain/schemas/predict.json").read_text(encoding="utf-8"))
BAR = timedelta(minutes=signals.BAR_MINUTES)
RULES = "bollinger %b<0→lên, >100→xuống · streak ≥6 nến giảm→lên, ≥6 tăng→xuống · rsi9 <20→lên, >80→xuống"
BACKTEST = (
    "Mỗi tín hiệu 53–57% đúng hướng nến 15m kế tiếp (2012–2026); chiều 'xuống' chỉ ~51% từ 2022. "
    "Không tín hiệu: ~50% (tung đồng xu)."
)


# ---------------------------------------------------------------------------- chọn mẫu
async def all_signal_bars(since: date, until: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Mọi nến 15m có tín hiệu trong khoảng + đường chuẩn trên toàn bộ. Một câu SQL, tính tín hiệu trong Python."""
    start = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(until, datetime.min.time(), tzinfo=UTC)
    async with SessionLocal() as s:
        bars = await candle_repo.live_bars(s, signals.BAR_MINUTES, end, start - timedelta(days=10), 10**7)
    out, base = [], {"n": 0, "correct": 0, "flat": 0, "mixed": 0, "by": {}}
    for i in range(signals.N_BARS, len(bars) - 1):
        cur, nxt = bars[i], bars[i + 1]
        bar_end = cur["ts"] + BAR
        if bar_end < start or nxt["ts"] != bar_end or not signals.auto_allowed(bar_end):
            continue  # nến kế tiếp phải liền kề (chấm được) và thị trường mở suốt 15 phút tới
        if bars[i - 1]["ts"] != cur["ts"] - BAR:
            continue  # như production: nến vừa đóng phải liền nến trước (không qua giờ nghỉ)
        ev = signals.evaluate([b["close"] for b in bars[i - signals.N_BARS + 1 : i + 1]])
        if not ev["has_signal"]:
            continue
        delta = nxt["close"] - cur["close"]
        if abs(delta) < 0.01:
            base["flat"] += 1
            continue
        actual = "up" if delta > 0 else "down"
        row = {"bar_end": bar_end.isoformat(), "close": cur["close"], "next_close": nxt["close"], "actual": actual}
        row.update({k: ev[k] for k in ("pct_b", "streak", "rsi9", "fired", "hint")})
        out.append(row)
        if ev["hint"] in ("up", "down"):
            base["n"] += 1
            base["correct"] += ev["hint"] == actual
            for f in ev["fired"]:
                k = f"{f['name']}:{f['hint']}"
                b = base["by"].setdefault(k, [0, 0])
                b[0] += 1
                b[1] += f["hint"] == actual
        else:
            base["mixed"] += 1
    return out, base


async def cmd_sample(a: argparse.Namespace) -> None:
    rows, base = await all_signal_bars(date.fromisoformat(a.since), date.fromisoformat(a.until))
    rng = random.Random(a.seed)
    picked = sorted(rng.sample(rows, min(a.n, len(rows))), key=lambda r: r["bar_end"])
    # Tách train/test xen kẽ theo thời gian (không để test dồn về cuối kỳ)
    for k, r in enumerate(picked):
        r["split"] = "test" if k % 2 else "train"
        r["offset"] = rng.randint(-700, 700)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in picked) + "\n", encoding="utf-8")
    rate = 100 * base["correct"] / base["n"] if base["n"] else None
    print(f"nến có tín hiệu: {len(rows)} (+{base['flat']} đứng giá, {base['mixed']} tín hiệu trái chiều)")
    print(f"ĐƯỜNG CHUẨN đi theo tín hiệu: {rate:.1f}% đúng trên {base['n']} nến  (±{ci(rate, base['n']):.1f})")
    for k, (n, c) in sorted(base["by"].items()):
        print(f"  {k:14} {100 * c / n:5.1f}% trên {n}")
    print(f"đã chọn {len(picked)} mẫu → {a.out}")


# ---------------------------------------------------------------------------- gói dữ liệu (ẩn danh)
def _sh(v: float | None, off: int) -> float | None:
    return round(v + off, 2) if v is not None else None


async def build_pack(r: dict[str, Any]) -> dict[str, Any]:
    """Gói giống `context_pack.build_prediction` nhưng không ngày, không FED, giá dời `offset`."""
    bar_end = datetime.fromisoformat(r["bar_end"])
    off = r["offset"]
    async with SessionLocal() as s:
        sig = await signals.compute(s, bar_end)
        ctx = await signals.context(s, bar_end, sig["last_close"])
    local = bar_end.astimezone(VN)
    recent = sig.pop("recent")
    n = len(recent)
    bars = [
        {"t": f"t-{n - k}", "o": _sh(b["o"], off), "h": _sh(b["h"], off), "l": _sh(b["l"], off), "c": _sh(b["c"], off)}
        for k, b in enumerate(recent)
    ]
    for rg in ctx["range"].values():
        rg["low"], rg["high"] = _sh(rg["low"], off), _sh(rg["high"], off)
    sig["last_close"] = _sh(sig["last_close"], off)
    for k in ("bar_end", "last_bar_ts"):
        sig.pop(k, None)
    return {
        "now_vn": f"{WEEKDAYS[local.weekday()]} {local:%H:%M} (giờ Việt Nam)",
        "note": "Bản tua lại: không có ngày tháng và lịch FED; mức giá đã dời — chỉ dùng số trong gói.",
        "live": {"price": sig["last_close"], "fresh": True, "market_open": True, "can_suggest": True},
        "signals_15m": {**sig, "rules": RULES, "backtest": BACKTEST},
        "bars_15m_recent": bars,
        "context_no_predictive_power": {
            **ctx,
            "note": "Vị trí trong biên 5/20/60 ngày và RSI 14 khung 1h/4h/1D KHÔNG đoán được 15 phút kế tiếp"
            " (backtest).",
        },
        "trigger": {"signals_at_trigger": {k: r[k] for k in ("pct_b", "streak", "rsi9", "fired", "hint")}},
    }


# ---------------------------------------------------------------------------- đi tới từng ngày (walk-forward)
async def build_live_pack(bar_end: datetime) -> dict[str, Any]:
    """Gói y như `context_pack.build_prediction` tại thời điểm `bar_end`: ngày giờ thật, FED tại thời điểm đó,
    chỉ nến đã đóng TRƯỚC `bar_end` — không có gì của tương lai."""
    async with SessionLocal() as s:
        sig = await signals.compute(s, bar_end)
        ctx = await signals.context(s, bar_end, sig["last_close"])
        fed = await fed_svc.pack(s, bar_end)
    local = bar_end.astimezone(VN)
    recent = sig.pop("recent")
    return {
        "now_vn": f"{WEEKDAYS[local.weekday()]} {local:%d/%m/%Y %H:%M}",
        "timezone": "Giờ Việt Nam (UTC+7)",
        "live": {"price": sig["last_close"], "fresh": True, "market_open": True, "can_suggest": True},
        "signals_15m": {**sig, "rules": RULES, "backtest": BACKTEST},
        "bars_15m_recent": recent,
        "context_no_predictive_power": {
            **ctx,
            "note": "Vị trí trong biên 5/20/60 ngày và RSI 14 khung 1h/4h/1D KHÔNG đoán được 15 phút kế tiếp"
            " (backtest).",
        },
        "fed": fed,
        "trigger": {"bar_end": bar_end.isoformat()},
    }


async def cmd_day(a: argparse.Namespace) -> None:
    last = date.fromisoformat(a.date)
    first = last - timedelta(days=a.days - 1)
    rows, _ = await all_signal_bars(first, last + timedelta(days=1))
    picks = [r for r in rows if signals.auto_trigger(r)]  # đúng luật production: ≥ 2 tín hiệu cùng hướng
    system = Path(a.prompt).read_text(encoding="utf-8")
    brain = ClaudeCli(settings.claude_cli_bin, a.model, settings.brain_timeout_seconds, a.effort)
    out = Path(f"data/eval/day-{first}-{last}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(a.concurrency)
    results: list[dict[str, Any]] = []

    async def one(r: dict[str, Any]) -> None:
        async with sem:
            be = datetime.fromisoformat(r["bar_end"])
            pack = await build_live_pack(be)
            try:
                rep_ = await brain.ask(system, json.dumps({"data": pack}, ensure_ascii=False, default=str), SCHEMA)
                d = rep_.data
                res = {**r, "direction": d.get("direction"), "confidence": d.get("confidence"),
                       "reason": d.get("reason"), "cost_usd": rep_.usage.get("cost_usd")}  # fmt: skip
            except BrainError as e:
                res = {**r, "error": e.message}
            results.append(res)

    print(f"{first} → {last}: {len(rows)} nến có tín hiệu, {len(picks)} nến đủ luật ≥ 2 tín hiệu → gọi bot", flush=True)
    await asyncio.gather(*(one(r) for r in picks))
    results.sort(key=lambda r: r["bar_end"])
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    ok = [r for r in results if r.get("direction")]
    print(f"\n{'giờ VN':12} {'tín hiệu':22} {'giá lúc đoán':>12} {'15 phút sau':>11}  bot     thật    kết quả")
    for r in results:
        t = datetime.fromisoformat(r["bar_end"]).astimezone(VN).strftime("%d/%m %H:%M")
        names = "+".join(f["name"] for f in r["fired"]) + (" ▲" if r["hint"] == "up" else " ▼")
        if r.get("error"):
            print(f"{t:12} {names:22} lỗi: {r['error'][:60]}")
            continue
        mark = "ĐÚNG" if r["direction"] == r["actual"] else "sai"
        arrow = {"up": "▲ tăng", "down": "▼ giảm"}
        print(f"{t:12} {names:22} {r['close']:12.2f} {r['next_close']:11.2f}  {arrow[r['direction']]:7} "
              f"{arrow[r['actual']]:7} {mark}  ({r['confidence']})")  # fmt: skip
    if ok:
        c = sum(r["direction"] == r["actual"] for r in ok)
        rule = sum(r["hint"] == r["actual"] for r in ok)
        cost = sum(r.get("cost_usd") or 0 for r in results)
        print(
            f"\nBot đúng {c}/{len(ok)} = {100 * c / len(ok):.0f}% · luật (theo tín hiệu) {rule}/{len(ok)}"
            f" · {cost:.2f} USD credit"
        )
    print(f"→ {out}")


# ---------------------------------------------------------------------------- chạy
async def cmd_run(a: argparse.Namespace) -> None:
    samples = [json.loads(x) for x in Path(a.samples).read_text(encoding="utf-8").splitlines() if x.strip()]
    samples = [r for r in samples if r["split"] == a.split][: a.limit or None]
    system = Path(a.prompt).read_text(encoding="utf-8")
    out = Path(a.out or f"data/eval/run-{a.name}-{a.split}.jsonl")
    done = {}
    if out.exists():  # chạy tiếp được sau khi dừng giữa chừng — không trả tiền hai lần cho cùng mẫu
        for x in out.read_text(encoding="utf-8").splitlines():
            if x.strip():
                d = json.loads(x)
                done[d["bar_end"]] = d
    todo = [r for r in samples if r["bar_end"] not in done]
    spent = sum(d.get("cost_usd") or 0 for d in done.values())
    brain = ClaudeCli(settings.claude_cli_bin, a.model, settings.brain_timeout_seconds, a.effort)
    sem = asyncio.Semaphore(a.concurrency)
    lock = asyncio.Lock()
    stop = False

    async def one(r: dict[str, Any]) -> None:
        nonlocal spent, stop
        async with sem:
            if stop:
                return
            pack = await build_pack(r)
            try:
                rep = await brain.ask(system, json.dumps({"data": pack}, ensure_ascii=False, default=str), SCHEMA)
                d = rep.data
                res = {
                    "bar_end": r["bar_end"], "actual": r["actual"], "hint": r["hint"], "fired": r["fired"],
                    "direction": d.get("direction"), "confidence": d.get("confidence"), "reason": d.get("reason"),
                    "cost_usd": rep.usage.get("cost_usd"), "seconds": rep.usage.get("seconds"),
                }  # fmt: skip
            except BrainError as e:
                res = {"bar_end": r["bar_end"], "actual": r["actual"], "hint": r["hint"], "error": e.message}
            async with lock:
                spent += res.get("cost_usd") or 0
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
                if spent >= a.budget:
                    stop = True
                    print(f"CHẠM TRẦN {a.budget} USD — dừng", flush=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"{a.name}/{a.split}: {len(todo)} mẫu cần chạy, đã có {len(done)}, đã tiêu {spent:.2f} USD", flush=True)
    await asyncio.gather(*(one(r) for r in todo))
    report([out])


# ---------------------------------------------------------------------------- báo cáo
def ci(rate: float | None, n: int) -> float:
    """Nửa khoảng tin cậy 95% (xấp xỉ chuẩn) theo điểm %."""
    if not n or rate is None:
        return float("nan")
    p = rate / 100
    return 196 * math.sqrt(p * (1 - p) / n)


def report(paths: list[Path]) -> None:
    for p in paths:
        rows = [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]
        ok = [r for r in rows if r.get("direction") in ("up", "down")]
        n = len(ok)
        c = sum(r["direction"] == r["actual"] for r in ok)
        hinted = [r for r in ok if r["hint"] in ("up", "down")]
        base = sum(r["hint"] == r["actual"] for r in hinted)
        follow = sum(r["direction"] == r["hint"] for r in hinted)
        cost = sum(r.get("cost_usd") or 0 for r in rows)
        secs = sorted(r["seconds"] for r in ok if r.get("seconds"))
        rate = 100 * c / n if n else None
        print(f"\n{Path(p).name}: {n} mẫu chấm được, {len(rows) - n} lỗi, {cost:.2f} USD")
        if n:
            print(f"  Claude: {rate:.1f}% đúng (±{ci(rate, n):.1f})")
        if hinted:
            br = 100 * base / len(hinted)
            print(f"  Đường chuẩn trên cùng mẫu: {br:.1f}% · Claude đi theo tín hiệu {100 * follow / len(hinted):.0f}%")
        for conf in ("low", "medium", "high"):
            sub = [r for r in ok if r.get("confidence") == conf]
            if sub:
                print(f"  độ tin cậy {conf}: {100 * sum(r['direction'] == r['actual'] for r in sub) / len(sub):.1f}%"
                      f" trên {len(sub)}")  # fmt: skip
        if secs:
            print(f"  thời gian: trung vị {secs[len(secs) // 2]:.0f}s, chậm nhất {secs[-1]:.0f}s")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(prog="python -m goldpulse.eval.replay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample")
    sp.add_argument("--since", default="2025-01-01")
    sp.add_argument("--until", default="2026-09-20")
    sp.add_argument("--n", type=int, default=300)
    sp.add_argument("--seed", type=int, default=20260926)
    sp.add_argument("--out", default="data/eval/samples.jsonl")
    rp = sub.add_parser("run")
    rp.add_argument("--samples", default="data/eval/samples.jsonl")
    rp.add_argument("--split", choices=["train", "test"], default="train")
    rp.add_argument("--prompt", default=str(ROOT / "src/goldpulse/brain/prompts/predict.md"))
    rp.add_argument("--name", required=True)
    rp.add_argument("--out")
    rp.add_argument("--limit", type=int, default=0)
    rp.add_argument("--budget", type=float, default=20.0)
    rp.add_argument("--concurrency", type=int, default=4)
    rp.add_argument("--model", default=settings.claude_cli_model)
    rp.add_argument("--effort", default=settings.claude_cli_effort)
    dp = sub.add_parser("day")
    dp.add_argument("--date", required=True, help="ngày cuối (UTC), vd 2026-09-25")
    dp.add_argument("--days", type=int, default=1)
    dp.add_argument("--prompt", default=str(ROOT / "src/goldpulse/brain/prompts/predict.md"))
    dp.add_argument("--concurrency", type=int, default=2)
    dp.add_argument("--model", default=settings.claude_cli_model)
    dp.add_argument("--effort", default=settings.claude_cli_effort)
    rep = sub.add_parser("report")
    rep.add_argument("paths", nargs="+")
    a = ap.parse_args()

    async def go() -> None:
        try:
            if a.cmd == "sample":
                await cmd_sample(a)
            elif a.cmd == "day":
                await cmd_day(a)
            elif a.cmd == "run":
                await cmd_run(a)
        finally:
            await engine.dispose()

    if a.cmd == "report":
        report([Path(p) for p in a.paths])
    else:
        asyncio.run(go())


if __name__ == "__main__":
    main()
