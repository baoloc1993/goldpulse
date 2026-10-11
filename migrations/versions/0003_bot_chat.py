"""bot chat: fed_decision · chat_thread · chat_message · bot_job · prediction (chỉ THÊM bảng)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fed_decision",
        sa.Column("meeting_date", sa.Date, primary_key=True),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate_lower", sa.Float, nullable=False),
        sa.Column("rate_upper", sa.Float, nullable=False),
        sa.Column("change_bp", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("source_note", sa.Text),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('scheduled', 'unscheduled')", name="ck_fed_kind"),
    )
    op.create_table(
        "chat_thread",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
    )
    op.create_table(
        "bot_job",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB),
        sa.Column("last_error", sa.Text),
        sa.CheckConstraint("kind IN ('chat', 'signal_prediction')", name="ck_job_kind"),
        sa.CheckConstraint("status IN ('pending', 'running', 'done', 'error')", name="ck_job_status"),
    )
    op.create_index("ix_bot_job_status_created", "bot_job", ["status", "created_at"])
    # Worker khởi động lại giữa nến không được đẻ job dự đoán trùng
    op.create_index(
        "ux_bot_job_signal_bar",
        "bot_job",
        [sa.text("(payload->>'bar_ts')")],
        unique=True,
        postgresql_where=sa.text("kind = 'signal_prediction'"),
    )
    op.create_table(
        "prediction",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("made_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False, server_default="low"),
        sa.Column("price_at", sa.Float, nullable=False),
        sa.Column("horizon_minutes", sa.Integer, nullable=False, server_default="15"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_due", sa.Float),
        sa.Column("outcome", sa.Text, nullable=False, server_default="pending"),
        sa.Column("signals", JSONB, nullable=False, server_default="{}"),
        sa.Column("reasoning", sa.Text, nullable=False, server_default=""),
        sa.Column("backend", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("job_id", sa.Integer),
        sa.Column("scored_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("source IN ('auto', 'chat')", name="ck_pred_source"),
        sa.CheckConstraint("direction IN ('up', 'down')", name="ck_pred_direction"),
        sa.CheckConstraint("confidence IN ('low', 'medium', 'high')", name="ck_pred_confidence"),
        sa.CheckConstraint("outcome IN ('pending', 'correct', 'wrong', 'flat', 'unscored')", name="ck_pred_outcome"),
    )
    op.create_index("ix_prediction_outcome_due", "prediction", ["outcome", "due_at"])
    op.create_index("ix_prediction_made_at", "prediction", ["made_at"])
    op.create_index(
        "ux_prediction_auto_made_at",
        "prediction",
        ["made_at"],
        unique=True,
        postgresql_where=sa.text("source = 'auto'"),
    )
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.Integer, sa.ForeignKey("chat_thread.id"), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="ok"),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("bot_job.id")),
        sa.Column("prediction_id", sa.Integer, sa.ForeignKey("prediction.id")),
        sa.Column("context_pack", JSONB),
        sa.Column("usage", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_msg_role"),
        sa.CheckConstraint("status IN ('ok', 'pending', 'error')", name="ck_msg_status"),
    )
    op.create_index("ix_chat_message_thread", "chat_message", ["thread_id", "id"])


def downgrade() -> None:
    # Chỉ bỏ bảng bot chat — kho dữ liệu không bị chạm. Sổ dự đoán mất theo (đó là dữ liệu của tính năng bị gỡ).
    op.drop_table("chat_message")
    op.drop_table("prediction")
    op.drop_table("bot_job")
    op.drop_table("chat_thread")
    op.drop_table("fed_decision")
