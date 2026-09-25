"""Cấu hình — thiếu biến bắt buộc thì app/worker KHÔNG khởi động."""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / "deployment" / "local" / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    database_url: str
    ops_access_key: str = Field(min_length=16)  # khoá ngắn + 10 lần thử/phút là dò được
    app_env: Literal["development", "test", "production"] = "development"
    sentry_dsn: str | None = None

    history_start_date: date = date(2004, 1, 1)
    realtime_poll_seconds: float = 5.0
    # Lấy bù HistData: HistData chỉ phát hành ~1 lần/tuần nên một lần/ngày là đủ (DECISIONS 2026-09-25)
    backfill_hours: float = 24.0
    # Chỉ bật khi web đứng sau Cloudflare Tunnel: tin header CF-Connecting-IP để giới hạn theo IP thật.
    # Không có Tunnel mà bật → người gọi tự đặt header để né giới hạn đăng nhập (DECISIONS 2026-09-25)
    trust_cloudflare_ip: bool = False
    # Số file Dukascopy tải song song — Dukascopy chậm 15–50s/file, hay trả 503 (DECISIONS 2026-09-24)
    dukascopy_concurrency: int = 3
    dukascopy_delay_seconds: float = 0.5
    # Proxy tuỳ chọn chỉ cho worker khi tới Dukascopy (vd. WARP chế độ proxy: socks5://127.0.0.1:40000)
    dukascopy_proxy: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def secure_cookie(self) -> bool:
        """Cookie chỉ gửi qua HTTPS — mặc định BẬT, chỉ tắt khi chạy local (http://127.0.0.1)."""
        return self.app_env not in ("development", "test")

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg2")


settings = Settings()  # type: ignore[call-arg]
