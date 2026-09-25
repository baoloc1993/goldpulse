"""Bảng DB (ARCHITECTURE §6). Mọi thời điểm là UTC timestamptz. Không dùng làm response."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Column, DateTime, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _ts(**kw: Any) -> Any:
    return Field(sa_column=Column(DateTime(timezone=True), **kw))


class Candle1m(SQLModel, table=True):
    __tablename__ = "candle_1m"
    ts: datetime = Field(sa_column=Column(DateTime(timezone=True), primary_key=True))
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    source: str = Field(sa_column=Column(Text, nullable=False))  # dukascopy | histdata | goldapi
    is_final: bool = False
    updated_at: datetime = _ts(nullable=False)


class SessionBar(SQLModel, table=True):
    __tablename__ = "session_bar"
    session: str = Field(sa_column=Column(Text, primary_key=True))  # asia | europe | us
    trade_date: date = Field(primary_key=True)
    open_ts: datetime = _ts(nullable=False)
    close_ts: datetime = _ts(nullable=False)
    open: float
    high: float
    low: float
    close: float
    range: float
    direction: int = Field(sa_column=Column(SmallInteger, nullable=False))
    minutes_present: int
    minutes_traded: int
    is_final: bool
    updated_at: datetime = _ts(nullable=False)


class CrawlDay(SQLModel, table=True):
    __tablename__ = "crawl_day"
    day: date = Field(primary_key=True)
    status: str = Field(sa_column=Column(Text, nullable=False, index=True))  # pending|ok|empty|error
    candles: int = 0
    traded_minutes: int = 0
    calendar_minutes: int = 0  # phút thị trường mở trong ngày (mẫu số độ phủ)
    covered_minutes: int = 0  # phút thị trường mở có nến
    missing_ranges: list[Any] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    attempts: int = 0
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    fetched_at: datetime | None = _ts(nullable=True)


class FeedSample(SQLModel, table=True):
    __tablename__ = "feed_sample"
    polled_at: datetime = Field(sa_column=Column(DateTime(timezone=True), primary_key=True))
    source_ts: datetime | None = _ts(nullable=True)
    price: float | None = None
    lag_seconds: float | None = None
    ok: bool


class QueryLog(SQLModel, table=True):
    __tablename__ = "query_log"
    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, autoincrement=True))
    created_at: datetime = _ts(nullable=False, index=True)
    params: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    duration_ms: float
    result_rows: int


class WorkerStatus(SQLModel, table=True):
    """Trạng thái worker cho S1: nguồn lịch sử tới được không, lần lấy bù cuối, lúc chạy lần đầu."""

    __tablename__ = "worker_status"
    key: str = Field(sa_column=Column(Text, primary_key=True))
    value: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = _ts(nullable=False)
