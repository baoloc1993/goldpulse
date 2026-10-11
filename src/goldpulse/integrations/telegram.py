"""(v3) Gửi tin Telegram qua Bot API — chỉ text, không ghi DB."""

import httpx

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, timeout: float = 10.0) -> None:
        self._token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict) -> dict:
        try:
            resp = await self._client.post(API.format(token=self._token, method=method), json=payload)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"{type(e).__name__}: {e}") from e
        if not data.get("ok"):
            # Không đưa URL (có token) vào lỗi — chỉ mô tả Telegram trả về
            raise TelegramError(f"{resp.status_code}: {data.get('description', 'lỗi không rõ')}")
        return data

    async def send(self, chat_id: int | str, text: str) -> None:
        await self._call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})

    async def first_private_chat(self) -> int | None:
        """Chat riêng đầu tiên đã nhắn bot — để không bắt chủ dự án tự tra chat id."""
        data = await self._call("getUpdates", {"allowed_updates": ["message"]})
        for u in data.get("result", []):
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and isinstance(chat.get("id"), int):
                return chat["id"]
        return None
