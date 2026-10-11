"""(v4) Tài khoản giấy BTC: mở theo tín hiệu giờ · đóng/cháy theo từng nến 1m · sổ ngày UTC · số cho S10/S11.

Mô phỏng ĐÚNG `research/btc_strategies.py::simulate` (DECISIONS 2026-10-03): vào ở close nến 1m cuối của giờ tín hiệu;
giá trị lệnh = đòn bẩy × số dư; đi từng nến 1m — lỗ tạm theo high/low ≥ ngưỡng cháy → mất trọn số dư; tới nến cuối
trước hạn → `horizon`; tới nến 23:59 UTC trước hạn → `day_close`. Tiền GIẢ — không import `integrations/`.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ...repositories import feed as feed_repo
from ...repositories import inst as repo
from ..calendar import VN
from . import SYMBOL
from . import strategies as strat

log = structlog.get_logger("btc")
MIN = timedelta(minutes=1)
STALE = timedelta(minutes=5)  # xử giờ tín hiệu muộn hơn → không vào giá cũ (skipped stale)
EXIT_GRACE = timedelta(minutes=10)  # nến đóng lệnh mãi không về (nguồn thiếu phút) → đóng ở nến cuối có
CURVE_DAYS = 20
LOOKBACK = timedelta(days=90)
MISSED_MAX = timedelta(hours=24)
STATUS_KEY = "btc"
TRADE_KEY = "btc_trade"  # nhịp tim vòng lệnh (M+N) — vòng giá sống mà vòng lệnh chết thì S10 phải báo đỏ
ALIVE_S = 120
LAG_LIMIT_S = 60
# PRD §6 vòng 4 — ghi trước khi nhìn số (chốt 2026-10-03)
GO_MIN_TRADES = 30
GO_MAX_BURN_RATIO = 0.20


def trade_date_of(ts: datetime) -> date:
    return ts.astimezone(UTC).date()


def day_end_candle(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 23, 59, tzinfo=UTC)


def cost(acc: dict[str, Any]) -> float:
    """Phí + trượt khứ hồi trên giá trị lệnh (0,14%)."""
    return 2 * (float(acc["fee_side"]) + float(acc["slip_side"]))


def pnl_of(acc: dict[str, Any], t: dict[str, Any], price: float, equity: float) -> float:
    gross = t["direction"] * (price / t["entry_price"] - 1)
    return round(max(t["notional"] * (gross - cost(acc)), -equity), 4)


async def _equity_today(session: AsyncSession, acc: dict[str, Any], day: date) -> tuple[float, bool]:
    d = await repo.day_get(session, acc["code"], day)
    if not d:
        return float(acc["start_equity"]), False
    return float(d["start_equity"]) + float(d["realized_pnl"]), bool(d["burned"])


# --- N. đóng lệnh ---------------------------------------------------------------------------------------------------


def exit_at(t: dict[str, Any]) -> datetime:
    """Giờ lệnh THỰC SỰ đóng theo luật (hạn, hoặc hết ngày UTC nếu hạn qua ngày) — để hiển thị, không phải due_at."""
    return _exit_plan(t)[0] + MIN


def _exit_plan(t: dict[str, Any]) -> tuple[datetime, str]:
    due_candle = t["due_at"] - MIN
    end = day_end_candle(t["trade_date"])
    return (due_candle, "horizon") if due_candle <= end else (end, "day_close")


async def _close(
    session: AsyncSession, acc: dict[str, Any], t: dict[str, Any], price: float, reason: str, at: datetime
) -> bool:
    equity, _ = await _equity_today(session, acc, t["trade_date"])
    if reason == "stopout":
        pnl = -round(equity, 4)
    else:
        pnl = pnl_of(acc, t, price, equity)
    burned = reason == "stopout" or equity + pnl <= 0
    if not await repo.close_trade(session, t["id"], exit_price=price, reason=reason, pnl=pnl, closed_at=at):
        return False
    await repo.day_ensure(session, acc["code"], t["trade_date"], float(acc["start_equity"]))
    await repo.day_apply(session, acc["code"], t["trade_date"], pnl, burned)
    return True


async def close_open(session: AsyncSession, now: datetime) -> int:
    """Luồng N: mỗi lệnh mở đi lần lượt nến 1m đã có sau nến vào — idempotent, làm lại từ đầu mỗi lượt (≤ 240 nến)."""
    accounts = {a["code"]: a for a in await repo.accounts(session, SYMBOL)}
    closed = 0
    for t in await repo.open_trades(session, SYMBOL):
        acc = accounts[t["account_code"]]
        entry_candle = t["opened_at"] - MIN
        exit_candle, exit_reason = _exit_plan(t)
        equity, _ = await _equity_today(session, acc, t["trade_date"])
        notional = float(t["notional"])
        burn_frac = (equity - notional * cost(acc)) / notional if notional else 0.0
        done = False
        last = None
        for k in await repo.candles_between(session, SYMBOL, entry_candle, min(exit_candle, now)):
            last = k
            adverse = (
                (t["entry_price"] - k["low"]) / t["entry_price"]
                if t["direction"] == 1
                else (k["high"] - t["entry_price"]) / t["entry_price"]
            )
            if adverse >= burn_frac:
                price = t["entry_price"] * (1 - t["direction"] * burn_frac)
                closed += await _close(session, acc, t, round(price, 2), "stopout", k["ts"] + MIN)
                done = True
                break
            if k["ts"] == exit_candle:
                closed += await _close(session, acc, t, float(k["close"]), exit_reason, k["ts"] + MIN)
                done = True
                break
        if not done and last is None and now >= exit_candle + MIN + EXIT_GRACE:
            # Mất SẠCH nến giữa giờ vào và giờ đóng (lỗ thủng) mà kho đã có nến sau đó → không kẹt `open` mãi:
            # đóng ở giá vào (không có giá nào khác), mất đúng phí. Hiếm; lấp lỗ thủng thật là việc của backfill.
            newer = await repo.latest_candle(session, SYMBOL, now)
            if newer is not None and newer["ts"] > exit_candle:
                log.warning("btc_no_candles_in_trade_window", trade_id=t["id"], exit_candle=exit_candle.isoformat())
                closed += await _close(session, acc, t, float(t["entry_price"]), exit_reason, exit_candle + MIN)
        if not done and last is not None and now >= exit_candle + MIN + EXIT_GRACE:
            # Nguồn thiếu đúng phút đóng lệnh → đóng ở nến cuối có trước đó. CHỈ khi nguồn đã đi qua phút đó (đã có nến
            # sau nó); chưa có thì là worker/Binance chết hoặc đang lấy bù → chờ, không đóng sớm ở giá cũ (review v4)
            newer = await repo.latest_candle(session, SYMBOL, now)
            if newer is not None and newer["ts"] > exit_candle:
                log.warning("btc_exit_candle_missing", trade_id=t["id"], exit_candle=exit_candle.isoformat())
                closed += await _close(session, acc, t, float(last["close"]), exit_reason, last["ts"] + MIN)
    return closed


# --- M. tín hiệu + mở lệnh ------------------------------------------------------------------------------------------


async def open_from_signal(
    session: AsyncSession, sig: dict[str, Any], entry_price: float, now: datetime
) -> tuple[int, int]:
    """Một giờ tín hiệu đã tính → mỗi tài khoản có tín hiệu một dòng inst_trade (open / skipped). Trả (mở, bỏ qua)."""
    bar_ts: datetime = sig["bar_ts"]
    opened_at = bar_ts + timedelta(hours=1)
    entry_candle = opened_at - MIN
    day = trade_date_of(entry_candle)
    if entry_candle >= day_end_candle(day):
        return 0, 0  # nến 23:00 — hạn trong ngày bằng 0, bản nghiên cứu cũng bỏ (`exit_i ≤ e`)
    opened = skipped = 0
    for acc in await repo.accounts(session, SYMBOL):
        st = acc["strategy"]
        direction = int(sig["fired"].get(st["kind"], 0))
        if not direction:
            continue
        equity, burned = await _equity_today(session, acc, day)
        reason = None
        if not acc["enabled"]:
            reason = "disabled"
        elif await repo.open_trade_of(session, acc["code"]):
            reason = "open_trade"
        elif burned or equity <= 0:
            reason = "burned"
        elif now - opened_at > STALE:
            reason = "stale"
        v = sig["values"]
        row = {
            "account_code": acc["code"],
            "bar_ts": bar_ts,
            "trade_date": day,
            "opened_at": opened_at,
            "direction": direction,
            "entry_price": entry_price,
            "notional": round(float(acc["leverage"]) * equity, 4),
            "due_at": opened_at + timedelta(hours=int(st["hold_hours"])),
            "status": "skipped" if reason else "open",
            "skip_reason": reason,
            "context": {
                "kind": st["kind"],
                "p_up": v.get("p_up"),
                "conf": v.get("conf"),
                "thr": strat.model().get(st["min_conf"]) if st.get("min_conf") else None,
                "ema_gap": v.get("ema_gap"),
                "z1h": v.get("z1h"),
                "bb_upper": v.get("bb_upper"),
                "bb_lower": v.get("bb_lower"),
                "bb_width_prev": v.get("bb_width_prev"),
                "bb_q20_prev": v.get("bb_q20_prev"),
                "equity": round(equity, 4),
            },
        }
        if reason is None:
            await repo.day_ensure(session, acc["code"], day, float(acc["start_equity"]))
        if await repo.insert_trade(session, row):
            if reason is None:
                opened += 1
            else:
                skipped += 1
    return opened, skipped


async def process_hours(session: AsyncSession, now: datetime) -> list[datetime]:
    """Luồng M: mọi giờ đã đóng trong 24 giờ qua chưa có inst_signal và đã có nến HH:59 → tính, ghi, mở lệnh.
    Đóng lệnh tới hạn TRƯỚC (lệnh hết hạn đúng giờ này nhường chỗ cho tín hiệu mới — bản nghiên cứu `e ≥ busy`)."""
    first, last = await repo.candle_span(session, SYMBOL)
    if not first or not last or last - first < timedelta(days=30):
        return []
    top = now.replace(minute=0, second=0, microsecond=0)
    have = await repo.signal_bars(session, SYMBOL, top - MISSED_MAX)
    done: list[datetime] = []
    # Mốc bắt đầu = giờ tín hiệu đầu tiên từng có (lần chạy đầu: giờ vừa đóng). Giờ TRƯỚC mốc chưa từng chạy nên không
    # phải "giờ bị lỡ" — S11 không rác; giờ SAU mốc mà worker vắng thì vẫn bù + ghi skipped(stale) tối đa 24 giờ.
    begin = await repo.first_signal_bar(session, SYMBOL) or (top - timedelta(hours=1))
    bar_ts = max(top - MISSED_MAX, begin)
    while bar_ts < top:
        if bar_ts not in have and bar_ts >= first + timedelta(days=30):
            entry = await repo.candle_at(session, SYMBOL, bar_ts + timedelta(minutes=59))
            if entry is not None:
                await close_open(session, now)
                bars = await repo.hourly_bars(session, SYMBOL, bar_ts - LOOKBACK, bar_ts + timedelta(hours=1))
                if len(bars) >= strat.MIN_BARS and bars[-1]["bar_ts"] == bar_ts:
                    sig = strat.evaluate(bars, only_last=True)[0]
                    await repo.insert_signal(
                        session,
                        SYMBOL,
                        bar_ts,
                        now,
                        sig["close"],
                        sig["values"],
                        sig["fired"],
                        strat.model()["name"],
                    )
                    await open_from_signal(session, sig, float(entry["close"]), now)
                    done.append(bar_ts)
        bar_ts += timedelta(hours=1)
    return done


# --- số cho S10 / S11 -----------------------------------------------------------------------------------------------


def verdict(t: dict[str, Any]) -> str:
    """PRD §6 vòng 4: go · pivot (lãi nhưng chưa đủ 30 lệnh, hoặc đủ mà cháy > 20% ngày) · none."""
    # Dấu lãi theo Σ các ngày (day_close vẫn tính vào số dư — PRD §6); số lệnh đánh giá thì LOẠI day_close
    if t["pnl"] <= 0:
        return "none"
    burn_ok = t["days"] == 0 or t["burned_days"] / t["days"] <= GO_MAX_BURN_RATIO
    return "go" if t["eval_n"] >= GO_MIN_TRADES and burn_ok else "pivot"


def _trade_view(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t["id"],
        "bar_vn": t["bar_ts"].astimezone(VN).strftime("%d/%m %H:%M"),
        "opened_vn": t["opened_at"].astimezone(VN).strftime("%d/%m %H:%M"),
        "due_vn": exit_at(t).astimezone(VN).strftime("%H:%M"),
        "closed_vn": t["closed_at"].astimezone(VN).strftime("%H:%M") if t["closed_at"] else None,
        "direction": t["direction"],
        "entry_price": t["entry_price"],
        "exit_price": t["exit_price"],
        "exit_reason": t["exit_reason"],
        "notional": t["notional"],
        "pnl": t["pnl"],
        "status": t["status"],
        "skip_reason": t["skip_reason"],
        "context": t["context"] or {},
    }


async def live(session: AsyncSession, now: datetime) -> dict[str, Any]:
    feed = await repo.latest_feed(session, SYMBOL)
    price = await repo.latest_price(session, SYMBOL)
    st = await feed_repo.get_status(session, STATUS_KEY) or {}
    tr = await feed_repo.get_status(session, TRADE_KEY) or {}
    first, last = await repo.candle_span(session, SYMBOL)
    lag = None
    if last is not None:
        # Nến cuối đã đóng ở last+1m; nến kế tiếp đáng lẽ có sau last+2m → chỉ tính phần TRỄ hơn mốc đó
        lag = max(0.0, (now - (last + 2 * MIN)).total_seconds())
    return {
        "price": price["price"] if price else None,
        "lag_seconds": round(lag) if lag is not None else None,
        "fresh": lag is not None and lag < LAG_LIMIT_S,
        "lag_limit": LAG_LIMIT_S,
        "error": "Binance không trả lời (chi tiết ở log worker)" if feed and not feed["ok"] else None,
        "worker_alive": bool(st.get("updated_at") and (now - st["updated_at"]).total_seconds() < ALIVE_S)
        and bool(tr.get("updated_at") and (now - tr["updated_at"]).total_seconds() < ALIVE_S),
        "history_days": round((last - first).total_seconds() / 86400, 1) if first and last else 0.0,
        "history_target": LOOKBACK.days,
        "next_check_vn": (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        .astimezone(VN)
        .strftime("%H:%M"),
    }


async def account_view(session: AsyncSession, acc: dict[str, Any], now: datetime, mark: float | None) -> dict[str, Any]:
    today = trade_date_of(now)
    day = await repo.day_get(session, acc["code"], today)
    start = float(acc["start_equity"])
    realized = float(day["realized_pnl"]) if day else 0.0
    burned = bool(day and day["burned"])
    open_t = await repo.open_trade_of(session, acc["code"])
    unrealized = None
    if open_t and mark is not None:
        unrealized = pnl_of(acc, open_t, mark, start + realized)
    tot = await repo.totals(session, acc["code"])
    return {
        "code": acc["code"],
        "name": acc["name"],
        "strategy": acc["strategy"],
        "enabled": acc["enabled"],
        "leverage": acc["leverage"],
        "cost_pct": round(100 * cost(acc), 3),
        "start_equity": start,
        "equity": round(max(0.0, start + realized + (unrealized or 0.0)), 2),
        "today": {
            "pnl": round(realized, 2),
            "trades": int(day["n_trades"]) if day else 0,
            "wins": int(day["n_wins"]) if day else 0,
            "burned": burned,
        },
        "open": {**_trade_view(open_t), "unrealized": unrealized, "mark": mark} if open_t else None,
        "total": {**tot, "verdict": verdict(tot)},
        "curve": [
            {"date": d["trade_date"].isoformat(), "end": float(d["end_equity"]), "burned": bool(d["burned"])}
            for d in await repo.days_recent(session, acc["code"], CURVE_DAYS)
        ],
    }


async def screen(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    """Dữ liệu S10."""
    now = now or datetime.now(UTC)
    lv = await live(session, now)
    accounts = [await account_view(session, a, now, lv["price"]) for a in await repo.accounts(session, SYMBOL)]
    return {
        "now_vn": now.astimezone(VN).strftime("%H:%M %d/%m"),
        "trade_date": trade_date_of(now).isoformat(),
        "live": lv,
        "model": {k: strat.model()[k] for k in ("name", "thr_top5", "thr_top10")},
        "today_pnl": round(
            sum(a["today"]["pnl"] + ((a["open"]["unrealized"] or 0.0) if a["open"] else 0.0) for a in accounts), 2
        ),
        "today_trades": sum(a["today"]["trades"] for a in accounts),
        "today_wins": sum(a["today"]["wins"] for a in accounts),
        "accounts": accounts,
        "thresholds": {"go_min_trades": GO_MIN_TRADES, "go_max_burn_ratio": GO_MAX_BURN_RATIO},
    }


async def account_screen(session: AsyncSession, code: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Dữ liệu S11."""
    now = now or datetime.now(UTC)
    acc = await repo.get_account(session, code)
    if not acc or acc["symbol"] != SYMBOL:
        return None
    lv = await live(session, now)
    view = await account_view(session, acc, now, lv["price"])
    view["live"] = lv
    view["thresholds"] = {"go_min_trades": GO_MIN_TRADES, "go_max_burn_ratio": GO_MAX_BURN_RATIO}
    view["model"] = {k: strat.model()[k] for k in ("thr_top5", "thr_top10")}
    view["rows"] = await trades_page(session, code, None)
    return view


async def trades_page(session: AsyncSession, code: str, before_id: int | None, limit: int = 50) -> list[dict[str, Any]]:
    return [_trade_view(t) for t in await repo.trades_page(session, code, before_id, limit)]


async def account_exists(session: AsyncSession, code: str) -> bool:
    acc = await repo.get_account(session, code)
    return acc is not None and acc["symbol"] == SYMBOL


async def set_enabled(session: AsyncSession, code: str, enabled: bool) -> bool:
    if not await account_exists(session, code):
        return False
    return await repo.set_enabled(session, code, enabled)
