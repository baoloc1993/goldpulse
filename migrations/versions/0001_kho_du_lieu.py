"""kho dữ liệu XAUUSD

Revision ID: 0001
Revises:
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "candle_1m",
        sa.Column("ts", TS, primary_key=True),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False, server_default="0"),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("is_final", sa.Boolean, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source IN ('dukascopy', 'goldapi')", name="ck_candle_source"),
        sa.CheckConstraint("is_final = (source = 'dukascopy')", name="ck_candle_final_source"),
    )
    op.create_table(
        "session_bar",
        sa.Column("session", sa.Text, primary_key=True),
        sa.Column("trade_date", sa.Date, primary_key=True),
        sa.Column("open_ts", TS, nullable=False),
        sa.Column("close_ts", TS, nullable=False),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("range", sa.Float, nullable=False),
        sa.Column("direction", sa.SmallInteger, nullable=False),
        sa.Column("minutes_present", sa.Integer, nullable=False),
        sa.Column("minutes_traded", sa.Integer, nullable=False),
        sa.Column("is_final", sa.Boolean, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("session IN ('asia', 'europe', 'us')", name="ck_session_name"),
    )
    op.create_table(
        "crawl_day",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("candles", sa.Integer, nullable=False, server_default="0"),
        sa.Column("traded_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("calendar_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("covered_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("missing_ranges", JSONB, nullable=False, server_default="[]"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("fetched_at", TS),
        sa.CheckConstraint("status IN ('pending', 'ok', 'empty', 'error')", name="ck_crawl_status"),
    )
    op.create_index("ix_crawl_day_status", "crawl_day", ["status"])
    op.create_table(
        "feed_sample",
        sa.Column("polled_at", TS, primary_key=True),
        sa.Column("source_ts", TS),
        sa.Column("price", sa.Float),
        sa.Column("lag_seconds", sa.Float),
        sa.Column("ok", sa.Boolean, nullable=False),
    )
    op.create_table(
        "query_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("result_rows", sa.Integer, nullable=False),
    )
    op.create_index("ix_query_log_created_at", "query_log", ["created_at"])
    op.create_table(
        "worker_status",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for t in ("worker_status", "query_log", "feed_sample", "crawl_day", "session_bar", "candle_1m"):
        op.drop_table(t)
