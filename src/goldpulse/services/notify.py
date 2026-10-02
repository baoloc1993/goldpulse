"""(v3) Soạn tin báo Telegram từ sổ giấy — chỉ đọc `paper_trade`/`paper_day`, không đổi luật (DECISIONS 2026-10-02).

Không gọi HTTP (ranh giới `services/`): worker vòng K lấy tin ở đây rồi gửi bằng `integrations/telegram.py`.
Mốc trong `worker_status.telegram`: `last_trade_id` (dòng sổ mới → tin vào lệnh, gộp theo dự đoán) và
`closed_ids` (lệnh đã báo đóng trong CLOSED_WINDOW). Mốc chỉ lưu sau khi gửi thành công.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import paper as repo
from .calendar import VN

STATE_KEY = "telegram"
CLOSED_WINDOW = timedelta(hours=6)
DIRECTION = {"up": "TĂNG", "down": "GIẢM"}
# Cùng chữ với S9 (`paper.js` SKIP)
SKIP = {
    "rule": "không khớp luật",
    "open_trade": "đang có lệnh mở",
    "margin": "không đủ ký quỹ",
    "burned": "đã cháy hôm nay",
    "disabled": "đang tắt",
    "market_closing": "sắp tới giờ nghỉ",
    "stale": "dự đoán cũ hơn 5 phút",
}
EXIT = {
    "horizon": "hết giờ giữ",
    "stopout": "số dư về 0",
    "market_close": "thị trường nghỉ",
    "disabled": "tài khoản tắt",
}


def _vn(ts: datetime) -> str:
    return ts.astimezone(VN).strftime("%H:%M")


def _usd(x: float) -> str:
    return f"{x:+.2f}".replace("-", "−")


def _label(t: dict[str, Any]) -> str:
    return f"{t['account_code']} {t['account_name']}"


def open_message(rows: list[dict[str, Any]]) -> str:
    """Một tin cho một dự đoán: tài khoản nào vào, tài khoản nào bỏ qua và vì sao."""
    first = rows[0]
    p_up = (first.get("context") or {}).get("p_up")
    sure = f" · bot chắc {100 * max(p_up, 1 - p_up):.0f}%" if p_up is not None else ""
    opened = [t for t in rows if t["status"] != "skipped"]
    skipped = [t for t in rows if t["status"] == "skipped"]
    head = "🟢" if opened else "⚪"
    lines = [
        f"{head} Bot đoán {DIRECTION.get(first['direction'], first['direction'])} · dự đoán #{first['prediction_id']}"
        f" · {_vn(first['opened_at'])}",
        f"Giá vào {first['entry_price']:.2f}{sure}",
    ]
    if opened:
        lines.append("Vào lệnh: " + " · ".join(f"{_label(t)} (đóng {_vn(t['due_at'])})" for t in opened))
    else:
        lines.append("Không tài khoản nào vào lệnh.")
    if skipped:
        lines.append(
            "Bỏ qua: " + " · ".join(f"{_label(t)} — {SKIP.get(t['skip_reason'], t['skip_reason'])}" for t in skipped)
        )
    return "\n".join(lines)


def close_message(t: dict[str, Any], day: dict[str, Any] | None, total_pnl: float) -> str:
    pnl = float(t["pnl"] or 0)
    reason = t["exit_reason"]
    icon = "🔥" if reason == "stopout" else "⏸" if reason in ("market_close", "disabled") else "✅" if pnl > 0 else "❌"
    lines = [
        f"{icon} {_label(t)} đóng {_usd(pnl)} USD",
        f"{DIRECTION.get(t['direction'], t['direction'])} {t['entry_price']:.2f} → {t['exit_price']:.2f}"
        f" · {EXIT.get(reason, reason)}",
    ]
    if reason == "market_close":
        lines.append("Không tính vào đánh giá 4 tuần.")
    if day:
        lines.append(
            f"Hôm nay {_usd(float(day['realized_pnl']))} (số dư {float(day['end_equity']):.2f})"
            f" · cộng dồn {_usd(total_pnl)}"
        )
        if day["burned"]:
            lines.append("🔥 Tài khoản cháy hôm nay — dừng tới 05:00.")
    return "\n".join(lines)


async def collect(
    session: AsyncSession, state: dict[str, Any] | None, now: datetime
) -> tuple[list[str], dict[str, Any]]:
    """(tin cần gửi, mốc mới). Chưa có mốc → đặt mốc = hiện tại, không gửi bù lịch sử."""
    since = now - CLOSED_WINDOW
    closed = await repo.closed_since(session, since)
    if not state or "last_trade_id" not in state:
        return [], {
            **(state or {}),
            "last_trade_id": await repo.max_trade_id(session),
            "closed_ids": [t["id"] for t in closed],
        }

    messages: list[str] = []
    new = await repo.trades_after(session, int(state["last_trade_id"]))
    by_pred: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for t in new:
        by_pred[t["prediction_id"]].append(t)
    for _, rows in sorted(by_pred.items()):
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
