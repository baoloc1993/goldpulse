"""Bảng DB. Mọi thời điểm là UTC timestamptz. Không dùng làm response."""

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
    """Trạng thái worker cho Tổng quan: nguồn lịch sử tới được không, lần lấy bù cuối, lúc chạy lần đầu.
    (v2) thêm key `brain` (nhịp tim não), `fed` (lần nạp FED), `signal` (tín hiệu nến 15m gần nhất)."""

    __tablename__ = "worker_status"
    key: str = Field(sa_column=Column(Text, primary_key=True))
    value: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = _ts(nullable=False)


# --- bot chat (migration 0003) ---------------------------------------------


class FedDecision(SQLModel, table=True):
    """Một kỳ họp FOMC: mức lãi suất mục tiêu sau kỳ họp + thay đổi so kỳ trước."""

    __tablename__ = "fed_decision"
    meeting_date: date = Field(primary_key=True)
    announced_at: datetime = _ts(nullable=False)  # 14:00 America/New_York
    rate_lower: float
    rate_upper: float
    change_bp: int
    kind: str = Field(sa_column=Column(Text, nullable=False))  # scheduled | unscheduled
    source_note: str | None = Field(default=None, sa_column=Column(Text))
    loaded_at: datetime = _ts(nullable=False)


class ChatThread(SQLModel, table=True):
    __tablename__ = "chat_thread"
    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, autoincrement=True))
    created_at: datetime = _ts(nullable=False)
    last_message_at: datetime = _ts(nullable=False)
    title: str = Field(default="", sa_column=Column(Text, nullable=False))


class BotJob(SQLModel, table=True):
    """Hàng chờ giữa web/worker và tiến trình não (`brain`)."""

    __tablename__ = "bot_job"
    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, autoincrement=True))
    kind: str = Field(sa_column=Column(Text, nullable=False))  # chat | signal_prediction
    payload: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    status: str = Field(default="pending", sa_column=Column(Text, nullable=False))
    attempts: int = 0
    created_at: datetime = _ts(nullable=False)
    started_at: datetime | None = _ts(nullable=True)
    finished_at: datetime | None = _ts(nullable=True)
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    last_error: str | None = Field(default=None, sa_column=Column(Text))


class Prediction(SQLModel, table=True):
    """Sổ chấm — bất biến sau khi chấm."""

    __tablename__ = "prediction"
    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, autoincrement=True))
    made_at: datetime = _ts(nullable=False)
    source: str = Field(sa_column=Column(Text, nullable=False))  # auto | chat
    direction: str = Field(sa_column=Column(Text, nullable=False))  # up | down
    confidence: str = Field(default="low", sa_column=Column(Text, nullable=False))
    price_at: float
    horizon_minutes: int = 15
    due_at: datetime = _ts(nullable=False)
    price_due: float | None = None
    outcome: str = Field(default="pending", sa_column=Column(Text, nullable=False))
    signals: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    reasoning: str = Field(default="", sa_column=Column(Text, nullable=False))
    backend: str | None = Field(default=None, sa_column=Column(Text))
    model: str | None = Field(default=None, sa_column=Column(Text))
    job_id: int | None = None
    scored_at: datetime | None = _ts(nullable=True)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_message"
    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, autoincrement=True))
    thread_id: int = Field(foreign_key="chat_thread.id")
    role: str = Field(sa_column=Column(Text, nullable=False))  # user | assistant
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    status: str = Field(default="ok", sa_column=Column(Text, nullable=False))  # ok | pending | error
    job_id: int | None = Field(default=None, foreign_key="bot_job.id")
    prediction_id: int | None = Field(default=None, foreign_key="prediction.id")
    context_pack: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    usage: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    created_at: datetime = _ts(nullable=False)
