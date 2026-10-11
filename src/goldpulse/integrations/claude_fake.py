"""Backend `fake`: câu trả lời mẫu ĐÚNG schema, dựng từ chính gói dữ liệu — cho `make test` và chạy thử.

Không gọi mạng, không tốn credit. Chỉ được bật khi APP_ENV ≠ production (brain chặn lúc khởi động).
Ép lỗi để thử trạng thái lỗi: câu hỏi chứa "#lỗi" (lỗi thử lại được) hoặc "#hết-credit" (lỗi không thử lại).
"""

import asyncio
import json
from typing import Any

from .claude_base import BrainError, BrainReply

TRADE_WORDS = ("đặt lệnh", "vào lệnh giúp", "mua giúp", "bán giúp", "lot")
OTHER_ASSETS = ("bitcoin", "btc", "eurusd", "cổ phiếu", "dầu", "chứng khoán")
DIRECTION_WORDS = ("tăng hay giảm", "nên mua", "nên bán", "lên hay xuống", "mua vào")


class ClaudeFake:
    name = "fake"
    model = "fake"

    def __init__(self, delay: float = 1.5) -> None:
        self.delay = delay

    async def close(self) -> None:
        return None

    async def ask(self, system: str, user: str, schema: dict[str, Any]) -> BrainReply:
        await asyncio.sleep(self.delay)
        req = json.loads(user)
        q = str(req.get("question") or "").lower()
        if "#hết-credit" in q:
            raise BrainError("Claude CLI báo lỗi: đã hết Agent SDK credit tháng này (giả lập).", retryable=False)
        if "#lỗi" in q:
            raise BrainError("Claude CLI không trả lời trong 120 giây (giả lập).")
        data = req.get("data") or {}
        props = schema.get("properties", {})
        if "answer" not in props:  # schema dự đoán tự động
            return BrainReply(data=self._predict(data), usage={"backend": self.name, "model": self.model})
        return BrainReply(data=self._chat(q, data), usage={"backend": self.name, "model": self.model})

    @staticmethod
    def _hint(data: dict[str, Any]) -> str:
        sig = data.get("signals_15m") or {}
        return "down" if sig.get("hint") == "down" else "up"

    def _predict(self, data: dict[str, Any]) -> dict[str, Any]:
        d = self._hint(data)
        return {"direction": d, "confidence": "low", "reason": f"(fake) Theo tín hiệu 15 phút đang bật: {d}."}

    def _chat(self, q: str, data: dict[str, Any]) -> dict[str, Any]:
        base = {"answer": "", "prediction": None, "refusal_kind": None, "suggests_trade": False}
        if any(w in q for w in TRADE_WORDS):
            return {**base, "refusal_kind": "trade_request", "answer": "(fake) Mình không đặt lệnh."}
        if any(w in q for w in OTHER_ASSETS):
            return {**base, "refusal_kind": "not_gold", "answer": "(fake) Ngoài vàng."}
        live = data.get("live") or {}
        sig = data.get("signals_15m") or {}
        fed = (data.get("fed") or {}).get("next_meeting") or {}
        price = live.get("price")
        if not price:
            first = "(fake) Chưa có giá realtime."
        elif live.get("fresh"):
            first = f"(fake) Giá hiện tại **{price:,.2f} USD** (gold-api)."
        else:
            first = f"(fake) Giá lúc {live.get('price_time_vn')}: **{price:,.2f} USD** (gold-api) — đang trễ."
        lines = [
            first,
            f"Tín hiệu 15 phút: %b = **{sig.get('pct_b')}**, chuỗi {sig.get('streak')} nến,"
            f" RSI 9 = **{sig.get('rsi9')}**"
            f" — {'có' if sig.get('has_signal') else 'không có'} tín hiệu.",
        ]
        if fed:
            lines.append(f"Kỳ họp FOMC kế tiếp: {fed.get('date')} (còn {fed.get('days')} ngày).")
        if any(w in q for w in DIRECTION_WORDS):
            if live.get("can_suggest"):
                d = self._hint(data)
                lines.append(f"Mình nghiêng về **{'tăng' if d == 'up' else 'giảm'}**, độ tin cậy thấp.")
                return {
                    **base,
                    "answer": "\n".join(lines),
                    "prediction": {"direction": d, "confidence": "low", "reason": "(fake) theo tín hiệu"},
                    "suggests_trade": True,
                }
            if live.get("no_suggest_reason") == "market_closed":
                lines.append(
                    f"Thị trường đang nghỉ (mở lại lúc {live.get('next_open_vn')}) — mình không gợi ý mua/bán."
                )
            else:
                lines.append("Giá realtime đang trễ nên mình không gợi ý mua/bán và không ghi dự đoán.")
        return {**base, "answer": "\n".join(lines)}
