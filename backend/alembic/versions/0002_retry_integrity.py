"""补充重试关联的复合外键，确保重试只指向同一会话、同一用户的原轮次。"""

from alembic import op

revision = "0002_retry_integrity"
down_revision = "0001_chat"
branch_labels = None
depends_on = None


def upgrade():
    """在原表上增加同会话、同用户的重试关联约束，提交时统一检查延迟外键。"""
    # 同时校验轮次、会话和钱包归属，单列外键不足以阻止跨用户关联。
    op.create_foreign_key(
        "chat_turn_retry_fk",
        "chat_turns",
        "chat_turns",
        ["retry_of", "session_id", "user_address"],
        ["id", "session_id", "user_address"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade():
    """仅移除本版本添加的约束，保留已有轮次和重试字段数据。"""
    op.drop_constraint("chat_turn_retry_fk", "chat_turns", type_="foreignkey")
