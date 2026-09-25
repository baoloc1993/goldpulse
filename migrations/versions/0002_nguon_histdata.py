"""nguồn lịch sử HistData (không cần WARP)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_candle_source", "candle_1m", type_="check")
    op.drop_constraint("ck_candle_final_source", "candle_1m", type_="check")
    op.create_check_constraint("ck_candle_source", "candle_1m", "source IN ('dukascopy', 'histdata', 'goldapi')")
    op.create_check_constraint("ck_candle_final_source", "candle_1m", "is_final = (source <> 'goldapi')")


def downgrade() -> None:
    # Xoá nến HistData thì ngày của chúng phải về hàng chờ + bỏ session_bar đã tính từ chúng — nếu không kho
    # hở vĩnh viễn (crawl_day vẫn ok nên luồng A không nạp lại). Nạp lại qua Dukascopy khi hạ về 0001.
    op.execute("""
        UPDATE crawl_day SET status = 'pending', attempts = 0
        WHERE day IN (SELECT DISTINCT (ts AT TIME ZONE 'UTC')::date FROM candle_1m WHERE source = 'histdata')
    """)
    op.execute("""
        DELETE FROM session_bar
        WHERE trade_date IN (SELECT DISTINCT (ts AT TIME ZONE 'UTC')::date FROM candle_1m WHERE source = 'histdata')
    """)
    op.execute("DELETE FROM candle_1m WHERE source = 'histdata'")
    op.drop_constraint("ck_candle_source", "candle_1m", type_="check")
    op.drop_constraint("ck_candle_final_source", "candle_1m", type_="check")
    op.create_check_constraint("ck_candle_source", "candle_1m", "source IN ('dukascopy', 'goldapi')")
    op.create_check_constraint("ck_candle_final_source", "candle_1m", "is_final = (source = 'dukascopy')")
