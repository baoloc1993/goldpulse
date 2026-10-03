"""(v4) Tin Telegram cho bot BTC riêng — chỉ đọc `inst_trade`/`inst_day`, cùng cơ chế mốc với bot vàng
(services/notify.py) nhưng state riêng `worker_status.btc_telegram` và token riêng (AC-20). Không gọi HTTP."""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...repositories import inst as repo
from ..calendar import VN
from . import SYMBOL

STATE_KEY = "btc_telegram"
CLOSED_WINDOW = timedelta(hours=6)
DIRECTION = {1: "MUA", -1: "BÁN"}
KIND = {
    "model": "mô hình chắc",
    "model_trend": "mô hình thuận xu hướng",
    "spike": "nến giật mạnh",
    "squeeze": "nén rồi bung",
}
SKIP = {
    "open_trade": "đang có lệnh mở",
    "burned": "đã cháy hôm nay",
    "disabled": "đang tắt",
    "stale": "máy xử muộn hơn 5 phút",
}
EXIT = {"horizon": "hết giờ giữ", "day_close": "hết ngày (06:59 sáng)", "stopout": "cháy — lỗ hết số dư"}


def _vn(ts: datetime) -> str:
    return ts.astimezone(VN).strftime("%H:%M")


def _usd(x: float) -> str:
    return f"{x:+.2f}".replace("-", "−")


def _label(t: dict[str, Any]) -> str:
    return f"{t['account_code'].removeprefix('BTC-')} {t['account_name']}"


def open_message(rows: list[dict[str, Any]]) -> str:
    """Một tin cho một giờ tín hiệu: tài khoản nào vào, tài khoản nào bỏ qua và vì sao."""
    first = rows[0]
    opened = [t for t in rows if t["status"] != "skipped"]
    skipped = [t for t in rows if t["status"] == "skipped"]
    lines = [
        f"{'🟢' if opened else '⚪'} BTC · giờ {_vn(first['bar_ts'])}–{_vn(first['opened_at'])}"
        f" · giá {first['entry_price']:,.2f}"
    ]
    for t in opened:
        lines.append(
            f"Vào: {_label(t)} {DIRECTION.get(t['direction'], '?')} · {KIND.get(t['context'].get('kind'), '')}"
            f" · lệnh {t['notional']:.2f} USD · đóng {_vn(t['due_at'])}"
        )
    for t in skipped:
        lines.append(
            f"Bỏ qua: {_label(t)} {DIRECTION.get(t['direction'], '?')} — {SKIP.get(t['skip_reason'], t['skip_reason'])}"
        )
    lines.append("Tiền giả.")
    return "\n".join(lines)


def close_message(t: dict[str, Any], day: dict[str, Any] | None, total_pnl: float) -> str:
    pnl = float(t["pnl"] or 0)
    reason = t["exit_reason"]
    icon = "🔥" if reason == "stopout" else "⏸" if reason == "day_close" else "✅" if pnl > 0 else "❌"
    lines = [
        f"{icon} BTC {_label(t)} đóng {_usd(pnl)} USD",
        f"{DIRECTION.get(t['direction'], '?')} {t['entry_price']:,.2f} → {t['exit_price']:,.2f}"
        f" · {EXIT.get(reason, reason)}",
    ]
    if reason == "day_close":
        lines.append("Không tính vào số lệnh đánh giá 8 tuần.")
    if day:
        lines.append(
            f"Hôm nay {_usd(float(day['realized_pnl']))} (số dư {float(day['end_equity']):.2f})"
            f" · cộng dồn {_usd(total_pnl)}"
        )
        if day["burned"]:
            lines.append("🔥 Tài khoản cháy hôm nay — dừng tới 07:00 sáng.")
    return "\n".join(lines)


async def collect(
    session: AsyncSession, state: dict[str, Any] | None, now: datetime
) -> tuple[list[str], dict[str, Any]]:
    """(tin cần gửi, mốc mới). Chưa có mốc → đặt mốc = hiện tại, không gửi bù lịch sử."""
    closed = await repo.closed_since(session, SYMBOL, now - CLOSED_WINDOW)
    if not state or "last_trade_id" not in state:
        return [], {
            **(state or {}),
            "last_trade_id": await repo.max_trade_id(session, SYMBOL),
            "closed_ids": [t["id"] for t in closed],
        }
    messages: list[str] = []
    new = await repo.trades_after(session, SYMBOL, int(state["last_trade_id"]))
    by_bar: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for t in new:
        by_bar[t["bar_ts"]].append(t)
    for _, rows in sorted(by_bar.items()):
        messages.append(open_message(rows))
    seen = set(state.get("closed_ids") or [])
    totals: dict[str, float] = {}
    for t in closed:
        if t["id"] in seen:
            continue
        code = t["account_code"]
        if code not in totals:
            totals[code] = (await repo.totals(session, code))["pnl"]
        day = await repo.day_get(session, code, t["trade_date"])
        messages.append(close_message(t, day, totals[code]))
    last_id = max([int(state["last_trade_id"])] + [t["id"] for t in new])
    return messages, {**state, "last_trade_id": last_id, "closed_ids": [t["id"] for t in closed]}
