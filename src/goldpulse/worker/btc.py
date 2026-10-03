"""(v4) Vòng lặp BTC của worker — chạy khi `INSTRUMENTS` chứa BTCUSDT (ARCHITECTURE §7 L–O). Không chạm bảng vàng.

L `price_loop`: nến 1m Binance mỗi BTC_POLL_SECONDS, lấy bù khi hở (kho rỗng → 90 ngày) · M+N `trade_loop`: mỗi 20 s
đóng lệnh tới hạn/cháy rồi tính giờ tín hiệu còn thiếu · O `notify_loop`: bot Telegram BTC riêng.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from ..config import settings
from ..db import session_scope
from ..integrations.binance import BinanceClient, BinanceError
from ..integrations.telegram import TelegramClient, TelegramError
from ..repositories import feed as feed_repo
from ..repositories import inst as repo
from ..services.btc import SYMBOL
from ..services.btc import notify as btc_notify
from ..services.btc import paper as btc_paper

if TYPE_CHECKING:
    from .__main__ import Worker

log = structlog.get_logger("worker.btc")
MIN = timedelta(minutes=1)
TRADE_TICK_S = 20


class BtcLoops:
    def __init__(self, w: "Worker") -> None:
        self.w = w
        self.client = BinanceClient(settings.binance_base_url)

    async def close(self) -> None:
        await self.client.close()

    async def _status(self, **value) -> None:
        async with session_scope() as s:
            await feed_repo.set_status(s, btc_paper.STATUS_KEY, value)

    # --- L ----------------------------------------------------------------------------------------------------------
    async def backfill(self) -> None:
        """Lấy bù từ nến cuối có (kho rỗng → 90 ngày trước) tới phút vừa đóng, 1000 nến/lần."""
        while not self.w.stop.is_set():
            async with session_scope() as s:
                first, last = await repo.candle_span(s, SYMBOL)
            now = datetime.now(UTC)
            start = (last + MIN) if last else (now - btc_paper.LOOKBACK).replace(second=0, microsecond=0)
            if start >= now.replace(second=0, microsecond=0) - MIN:
                return
            closed, _, _ = await self.client.klines(SYMBOL, start=start, limit=1000)
            if not closed:
                return
            async with session_scope() as s:
                await repo.upsert_candles(s, SYMBOL, [(k.ts, k.open, k.high, k.low, k.close, k.volume) for k in closed])
            got_first = first or closed[0].ts
            days = (closed[-1].ts - got_first).total_seconds() / 86400
            await self._status(state="backfill", history_days=round(days, 1), last_ts=closed[-1].ts.isoformat())
            log.info("btc_backfill", upto=closed[-1].ts.isoformat(), n=len(closed))

    async def price_loop(self) -> None:
        while not self.w.stop.is_set():
            polled = datetime.now(UTC)
            try:
                await self.backfill()
                closed, live, server_now = await self.client.klines(SYMBOL, limit=3)
                async with session_scope() as s:
                    await repo.upsert_candles(
                        s, SYMBOL, [(k.ts, k.open, k.high, k.low, k.close, k.volume) for k in closed]
                    )
                    _, last = await repo.candle_span(s, SYMBOL)
                    price = live.close if live else (closed[-1].close if closed else None)
                    lag = max(0.0, (server_now - (last + 2 * MIN)).total_seconds()) if last else None
                    await repo.insert_feed(s, SYMBOL, polled, price, last, lag, True)
                await self._status(state="ok", last_ts=last.isoformat() if last else None)
                wait = settings.btc_poll_seconds
            except BinanceError as e:
                log.warning("btc_price_error", error=str(e))
                async with session_scope() as s:
                    await repo.insert_feed(s, SYMBOL, polled, None, None, None, False, str(e)[:300])
                await self._status(state="error", error=str(e)[:300])
                wait = max(settings.btc_poll_seconds, e.retry_after or 0)
            await self.w.sleep(wait)

    # --- M + N ------------------------------------------------------------------------------------------------------
    async def trade_loop(self) -> None:
        while not self.w.stop.is_set():
            now = datetime.now(UTC)
            async with session_scope() as s:
                n_closed = await btc_paper.close_open(s, now)
            async with session_scope() as s:
                hours = await btc_paper.process_hours(s, now)
            if n_closed or hours:
                log.info("btc_tick", closed=n_closed, hours=[h.isoformat() for h in hours])
            await self.w.sleep(TRADE_TICK_S)

    # --- O ----------------------------------------------------------------------------------------------------------
    async def notify_loop(self) -> None:
        if not settings.telegram_btc_bot_token:
            log.info("btc_telegram_off")
            await self.w.stop.wait()
            return
        tg = TelegramClient(settings.telegram_btc_bot_token)
        try:
            while not self.w.stop.is_set():
                await self.notify_once(tg)
                await self.w.sleep(15)
        finally:
            await tg.close()

    async def notify_once(self, tg: TelegramClient) -> None:
        async with session_scope() as s:
            raw = await feed_repo.get_status(s, btc_notify.STATE_KEY)
        state = {k: v for k, v in (raw or {}).items() if k != "updated_at"} or None
        chat_id = settings.telegram_btc_chat_id or (state or {}).get("chat_id")
        if chat_id is None:
            try:
                chat_id = await tg.first_private_chat()
                if chat_id is None:
                    return  # chủ dự án chưa nhắn bot BTC lần nào
                await tg.send(
                    chat_id,
                    "✅ GoldPulse BTC đã nối Telegram — sẽ báo khi tài khoản giấy BTC vào/đóng lệnh (tiền giả).",
                )
            except TelegramError as e:
                log.warning("btc_telegram_error", error=str(e))
                return
            state = {**(state or {}), "chat_id": chat_id}
            async with session_scope() as s:
                await feed_repo.set_status(s, btc_notify.STATE_KEY, state)
        async with session_scope() as s:
            messages, new_state = await btc_notify.collect(s, state, datetime.now(UTC))
        for m in messages:
            try:
                await tg.send(chat_id, m)
            except TelegramError as e:  # giữ mốc cũ → lượt sau gửi lại
                log.warning("btc_telegram_error", error=str(e))
                return
        async with session_scope() as s:
            await feed_repo.set_status(s, btc_notify.STATE_KEY, new_state)
        if messages:
            log.info("btc_telegram_sent", n=len(messages))

    async def cleanup_loop(self) -> None:
        while not self.w.stop.is_set():
            async with session_scope() as s:
                await repo.cleanup_feed(s)
            await self.w.sleep(86400)
