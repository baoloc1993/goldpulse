"""Tài khoản giấy (v3): 4 luật vào lệnh · mở theo dự đoán tự động · đóng/cháy mỗi phút · sổ ngày · số cho S8/S9.

ARCHITECTURE §6c + §7 I/J, PRD §7 "Tài khoản giấy". Tiền GIẢ — mọi thứ là hàng trong Postgres, module này không
import `integrations/` và không có cấu hình sàn nào. Giá vào = `prediction.price_at`; giá ra = cùng quy tắc chấm dự
đoán (`predictions.price_at(due_at)`); không mô phỏng trượt giá ngoài spread (DECISIONS 2026-09-27).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import candles as candle_repo
from ..repositories import feed as feed_repo
from ..repositories import paper as repo
from . import predictions
from .calendar import NY, VN, is_market_open

STALE_S = 300  # dự đoán quá 5 phút tuổi (worker vắng) → không vào giá cũ
GRACE_S = predictions.SCORE_GRACE_S  # nến phút cuối trước hạn đã ghi
MTM_MAX_AGE = timedelta(minutes=2)  # kiểm cháy chỉ bằng nến còn tươi — nguồn chết thì chờ, không cháy ảo
CURVE_DAYS = 20
STATUS_KEY = "paper"
# PRD §6 vòng 3 — ghi trước khi nhìn số
GO_MIN_TRADES = 40
GO_MAX_BURN_RATIO = 0.20


def trade_date_of(ts: datetime) -> date:
    """Ngày giao dịch NY: 18:00 hôm trước → 17:00 hôm nay = ngày NY của giờ đóng."""
    return (ts.astimezone(NY) + timedelta(hours=6)).date()


def rule_allows(rule: dict[str, Any], signals: dict[str, Any]) -> bool:
    kind = rule.get("kind", "all")
    if kind == "volatility":
        vol = signals.get("vol96")
        return vol is not None and float(vol) >= float(rule["min_vol"])
    if kind == "confidence":
        p = (signals.get("model") or {}).get("p_up")
        return p is not None and abs(float(p) - 0.5) >= float(rule["min_conf"])
    return True


def pnl_of(acc: dict[str, Any], direction: str, entry: float, exit_price: float) -> float:
    move = (exit_price - entry) if direction == "up" else (entry - exit_price)
    return round(acc["oz"] * (move - acc["spread"]), 4)


async def _equity_today(session: AsyncSession, acc: dict[str, Any], day: date) -> tuple[float, bool]:
    d = await repo.day_get(session, acc["code"], day)
    if not d:
        return float(acc["start_equity"]), False
    return float(d["start_equity"]) + float(d["realized_pnl"]), bool(d["burned"])


async def _decide(session: AsyncSession, acc: dict[str, Any], p: dict[str, Any], now: datetime) -> str | None:
    """None = mở lệnh; chuỗi = lý do bỏ qua (thứ tự: luật → tài khoản → giờ → tuổi dự đoán)."""
    if not rule_allows(acc["rule"], p["signals"] or {}):
        return "rule"
    if not acc["enabled"]:
        return "disabled"
    if await repo.open_trade_of(session, acc["code"]):
        return "open_trade"
    equity, burned = await _equity_today(session, acc, trade_date_of(p["made_at"]))
    if burned:
        return "burned"
    if equity < p["price_at"] / acc["leverage"]:
        return "margin"
    horizon = int(acc["rule"].get("horizon_minutes", 15))
    due = p["made_at"] + timedelta(minutes=horizon)
    if not (is_market_open(p["made_at"]) and is_market_open(due + timedelta(minutes=5))):
        return "market_closing"
    if (now - p["made_at"]).total_seconds() > STALE_S:
        return "stale"
    return None


async def open_for_new(session: AsyncSession, now: datetime | None = None) -> int:
    """Luồng I: mọi prediction(auto) mới kể từ lần xử trước → mỗi tài khoản một dòng paper_trade (open hoặc skipped).
    Trả số lệnh mở."""
    now = now or datetime.now(UTC)
    st = await feed_repo.get_status(session, STATUS_KEY)
    last_id = int((st or {}).get("last_prediction_id") or 0)
    preds = await repo.new_auto_predictions(session, last_id)
    if not preds:
        return 0
    accounts = await repo.accounts(session)
    opened = 0
    for p in preds:
        day = trade_date_of(p["made_at"])
        for acc in accounts:
            reason = await _decide(session, acc, p, now)
            horizon = int(acc["rule"].get("horizon_minutes", 15))
            sig = p["signals"] or {}
            row = {
                "account_code": acc["code"],
                "prediction_id": p["id"],
                "trade_date": day,
                "opened_at": p["made_at"],
                "direction": p["direction"],
                "oz": acc["oz"],
                "entry_price": p["price_at"],
                "spread": acc["spread"],
                "due_at": p["made_at"] + timedelta(minutes=horizon),
                "status": "skipped" if reason else "open",
                "skip_reason": reason,
                "context": {
                    "p_up": (sig.get("model") or {}).get("p_up"),
                    "vol96": sig.get("vol96"),
                    "fired": [f.get("name") for f in (sig.get("fired") or []) if isinstance(f, dict)],
                },
            }
            if reason is None:
                await repo.day_ensure(session, acc["code"], day, float(acc["start_equity"]))
            if await repo.insert_trade(session, row) and reason is None:
                opened += 1
        last_id = p["id"]
    await feed_repo.set_status(session, STATUS_KEY, {"last_prediction_id": last_id, "at": now.isoformat()})
    return opened


async def _close(
    session: AsyncSession, acc: dict[str, Any], t: dict[str, Any], price: float, reason: str, at: datetime
):
    pnl = pnl_of(acc, t["direction"], t["entry_price"], price)
    if await repo.close_trade(session, t["id"], exit_price=price, reason=reason, pnl=pnl, closed_at=at):
        await repo.day_ensure(session, acc["code"], t["trade_date"], float(acc["start_equity"]))
        await repo.day_apply(session, acc["code"], t["trade_date"], pnl, reason == "stopout")


async def close_due(session: AsyncSession, now: datetime | None = None) -> int:
    """Luồng J: tới hạn → đóng ở giá `price_due`; chưa tới hạn → kiểm cháy bằng nến tươi, thị trường đóng → đóng."""
    now = now or datetime.now(UTC)
    accounts = {a["code"]: a for a in await repo.accounts(session)}
    closed = 0
    for t in await repo.open_trades(session):
        acc = accounts[t["account_code"]]
        if now >= t["due_at"] + timedelta(seconds=GRACE_S):
            p1 = await predictions.price_at(session, t["due_at"])
            if p1 is not None:
                await _close(session, acc, t, p1, "horizon", t["due_at"])
            else:  # không có nến trong 5 phút trước hạn (đóng cửa / worker chết) → nến cuối có
                floor = t["due_at"].replace(second=0, microsecond=0)
                c = await candle_repo.close_before(session, floor, floor - timedelta(days=3))
                if c:
                    await _close(session, acc, t, float(c["close"]), "market_close", t["due_at"])
            closed += 1
            continue
        c = await repo.latest_close(session, now - MTM_MAX_AGE, now)
        if not c:
            continue  # nguồn giá đứng — không cháy ảo, không đóng (ARCHITECTURE §6 ca biên)
        if not is_market_open(now):
            await _close(session, acc, t, float(c["close"]), "market_close", now)
            closed += 1
            continue
        equity, _ = await _equity_today(session, acc, t["trade_date"])
        if equity + pnl_of(acc, t["direction"], t["entry_price"], float(c["close"])) <= 0:
            await _close(session, acc, t, float(c["close"]), "stopout", now)
            closed += 1
    return closed


async def tick(session: AsyncSession, now: datetime | None = None) -> tuple[int, int]:
    """Một nhịp worker: đóng trước (giải phóng chỗ), rồi mở theo dự đoán mới."""
    closed = await close_due(session, now)
    opened = await open_for_new(session, now)
    return opened, closed


def verdict(t: dict[str, Any]) -> str:
    """PRD §6 vòng 3 — trên lệnh đã đóng trừ `market_close`: go · pivot (lãi nhưng chưa đủ 40) · none."""
    if t["eval_pnl"] <= 0:
        return "none"
    burn_ok = t["days"] == 0 or t["burned_days"] / t["days"] <= GO_MAX_BURN_RATIO
    return "go" if t["eval_n"] >= GO_MIN_TRADES and burn_ok else "pivot"


def _trade_view(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t["id"],
        "prediction_id": t["prediction_id"],
        "opened_vn": t["opened_at"].astimezone(VN).strftime("%d/%m %H:%M"),
        "due_vn": t["due_at"].astimezone(VN).strftime("%H:%M"),
        "closed_vn": t["closed_at"].astimezone(VN).strftime("%H:%M") if t["closed_at"] else None,
        "direction": t["direction"],
        "entry_price": t["entry_price"],
        "exit_price": t["exit_price"],
        "exit_reason": t["exit_reason"],
        "pnl": t["pnl"],
        "status": t["status"],
        "skip_reason": t["skip_reason"],
        "context": t["context"] or {},
        "spread": t["spread"],
        "oz": t["oz"],
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
        unrealized = pnl_of(acc, open_t["direction"], open_t["entry_price"], mark)
    equity = max(0.0, start + realized + (unrealized or 0.0))
    tot = await repo.totals(session, acc["code"])
    return {
        "code": acc["code"],
        "name": acc["name"],
        "rule": acc["rule"],
        "enabled": acc["enabled"],
        "oz": acc["oz"],
        "spread": acc["spread"],
        "leverage": acc["leverage"],
        "start_equity": start,
        "equity": round(equity, 2),
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
    """Dữ liệu S8."""
    from .chat import worker_alive  # chat import predictions → tránh vòng lặp ở mức module
    from .context_pack import live_state

    now = now or datetime.now(UTC)
    live = await live_state(session, now.astimezone(VN))
    c = await repo.latest_close(session, now - timedelta(days=3), now)
    mark = float(c["close"]) if c else None
    accounts = [await account_view(session, a, now, mark) for a in await repo.accounts(session)]
    return {
        "now_vn": now.astimezone(VN).strftime("%H:%M %d/%m"),
        "trade_date": trade_date_of(now).isoformat(),
        "live": {k: live[k] for k in ("price", "lag_seconds", "fresh", "market_open", "price_time_vn", "lag_limit")},
        "worker_alive": await worker_alive(session, now),
        "today_pnl": round(
            sum(a["today"]["pnl"] + (a["open"]["unrealized"] or 0.0 if a["open"] else 0.0) for a in accounts), 2
        ),
        "today_trades": sum(a["today"]["trades"] for a in accounts),
        "today_wins": sum(a["today"]["wins"] for a in accounts),
        "accounts": accounts,
        "thresholds": {"go_min_trades": GO_MIN_TRADES, "go_max_burn_ratio": GO_MAX_BURN_RATIO},
    }


async def account_screen(session: AsyncSession, code: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Dữ liệu S9."""
    now = now or datetime.now(UTC)
    acc = await repo.get_account(session, code)
    if not acc:
        return None
    c = await repo.latest_close(session, now - timedelta(days=3), now)
    view = await account_view(session, acc, now, float(c["close"]) if c else None)
    view["rows"] = await trades_page(session, code, None)
    return view


async def trades_page(session: AsyncSession, code: str, before_id: int | None, limit: int = 50) -> list[dict[str, Any]]:
    return [_trade_view(t) for t in await repo.trades_page(session, code, before_id, limit)]


async def account_exists(session: AsyncSession, code: str) -> bool:
    return await repo.get_account(session, code) is not None


async def set_enabled(session: AsyncSession, code: str, enabled: bool) -> bool:
    return await repo.set_enabled(session, code, enabled)


async def pack(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """Khối `paper` trong gói dữ liệu cho não — 4 hàng tóm tắt, nói rõ tiền giả (AC-15)."""
    c = await repo.latest_close(session, now - timedelta(days=3), now)
    mark = float(c["close"]) if c else None
    out = []
    for a in await repo.accounts(session):
        v = await account_view(session, a, now, mark)
        out.append(
            {
                "code": v["code"],
                "name": v["name"],
                "enabled": v["enabled"],
                "equity_now_usd": v["equity"],
                "today_pnl_usd": v["today"]["pnl"],
                "today_trades": v["today"]["trades"],
                "total_pnl_usd": round(v["total"]["pnl"], 2),
                "total_trades": v["total"]["trades"],
                "burned_days": f"{v['total']['burned_days']}/{v['total']['days']}",
            }
        )
    return {
        "note": "Tài khoản GIẤY — tiền giả, sổ ghi trong hệ thống, không nối sàn nào. Mỗi tài khoản 10 USD, "
        "bắt đầu lại mỗi ngày giao dịch; mỗi tài khoản một luật vào lệnh theo dự đoán tự động.",
        "accounts": out,
    }
