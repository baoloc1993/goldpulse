"""Backend `api`: SDK `anthropic`, cần ANTHROPIC_API_KEY. Structured output theo schema, adaptive thinking.

Bật fallback phía máy chủ (`fallbacks: "default"`) — nếu model từ chối vì bộ lọc an toàn, API tự chạy lại trên
model dự phòng trong cùng lời gọi thay vì trả lỗi.
"""

import json
import time
from typing import Any

import anthropic

from .claude_base import BrainError, BrainReply


class ClaudeApi:
    name = "api"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        self.model = model
        # SDK tự thử lại 429/5xx/lỗi mạng 2 lần (ARCHITECTURE §9)
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)

    async def close(self) -> None:
        await self.client.close()

    async def ask(self, system: str, user: str, schema: dict[str, Any]) -> BrainReply:
        t0 = time.perf_counter()
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                # medium: câu trả lời phải về trong ≤ 60 giây (AC-6)
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise BrainError("ANTHROPIC_API_KEY không hợp lệ — kiểm tra khoá trong .env.", False) from e
        except anthropic.PermissionDeniedError as e:
            raise BrainError(f"Khoá API không có quyền dùng model {self.model}.", False) from e
        except anthropic.BadRequestError as e:
            msg = e.message
            fatal = "credit" in msg.lower() or "billing" in msg.lower()
            raise BrainError(f"Anthropic API từ chối yêu cầu: {msg}", retryable=not fatal) from e
        except anthropic.RateLimitError as e:
            raise BrainError("Anthropic API đang giới hạn tốc độ — thử lại sau ít phút.") from e
        except anthropic.APITimeoutError as e:
            raise BrainError("Anthropic API không trả lời kịp.") from e
        except anthropic.APIConnectionError as e:
            raise BrainError("Không kết nối được Anthropic API — kiểm tra mạng máy chủ.") from e
        except anthropic.APIStatusError as e:
            raise BrainError(f"Anthropic API lỗi {e.status_code}: {e.message}") from e
        if resp.stop_reason == "refusal":
            raise BrainError("Claude từ chối trả lời câu này.", retryable=False)
        if resp.stop_reason == "max_tokens":
            raise BrainError("Câu trả lời của Claude bị cắt giữa chừng.")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BrainError("Anthropic API không trả JSON đúng schema.") from e
        return BrainReply(
            data=data,
            usage={
                "backend": self.name,
                "model": resp.model,
                "seconds": round(time.perf_counter() - t0, 1),
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "request_id": resp._request_id,
            },
        )
