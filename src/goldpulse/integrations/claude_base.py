"""Interface chung của ba backend não (ARCHITECTURE §8): ask(system, user, schema) -> BrainReply.

Não KHÔNG có tool — chỉ nhận văn bản và trả JSON đúng schema.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class BrainError(Exception):
    """Lỗi gọi não. `message` là câu tiếng Việt hiện cho người dùng; `retryable` = nên thử lại lượt sau."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass
class BrainReply:
    data: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)


class Brain(Protocol):
    name: str
    model: str | None

    async def ask(self, system: str, user: str, schema: dict[str, Any]) -> BrainReply: ...

    async def close(self) -> None: ...
