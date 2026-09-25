"""Backend `cli`: subprocess `claude -p` bằng đăng nhập Claude Code của chủ dự án (gói Max 5x).

Không tool (`--tools ""`), không MCP, không lưu phiên; câu hỏi + gói dữ liệu đi qua stdin (dòng lệnh Windows
giới hạn ~32k ký tự); chạy trong thư mục trống để không nạp CLAUDE.md của repo.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from .claude_base import BrainError, BrainReply


class ClaudeCli:
    name = "cli"

    def __init__(self, bin_path: str, model: str, timeout: float) -> None:
        self.bin = bin_path
        self.model = model
        self.timeout = timeout
        self.cwd = Path(tempfile.gettempdir()) / "goldpulse-brain"
        self.cwd.mkdir(exist_ok=True)

    async def close(self) -> None:
        return None

    async def ask(self, system: str, user: str, schema: dict[str, Any]) -> BrainReply:
        args = [
            self.bin, "-p", "--output-format", "json", "--model", self.model,
            "--tools", "", "--strict-mcp-config", "--no-session-persistence",
            "--system-prompt", system, "--json-schema", json.dumps(schema, ensure_ascii=False),
        ]  # fmt: skip
        t0 = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise BrainError(
                f"Không tìm thấy lệnh `{self.bin}` trên máy chủ — cài Claude Code hoặc đặt CLAUDE_CLI_BIN.", False
            ) from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), self.timeout)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise BrainError(f"Claude CLI không trả lời trong {self.timeout:.0f} giây.") from e
        elapsed = round(time.perf_counter() - t0, 1)
        try:
            body = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            tail = (err or out).decode("utf-8", "replace").strip()[-300:]
            raise BrainError(f"Claude CLI trả về không phải JSON (mã {proc.returncode}): {tail}") from e
        if body.get("is_error"):
            msg = str(body.get("result") or body.get("terminal_reason") or "lỗi không rõ")
            # Hết hạn đăng nhập / hết credit: thử lại cũng vô ích tới khi chủ dự án xử lý
            fatal = any(k in msg.lower() for k in ("authenticate", "oauth", "credit", "login", "limit"))
            raise BrainError(f"Claude CLI báo lỗi: {msg}", retryable=not fatal)
        data = body.get("structured_output")
        if data is None:
            try:
                data = json.loads(body.get("result") or "")
            except (json.JSONDecodeError, TypeError) as e:
                raise BrainError("Claude CLI không trả JSON đúng schema.") from e
        usage = body.get("usage") or {}
        models = list((body.get("modelUsage") or {}).keys())
        return BrainReply(
            data=data,
            usage={
                "backend": self.name,
                "model": models[0] if models else self.model,
                "seconds": elapsed,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cost_usd": body.get("total_cost_usd"),
            },
        )
