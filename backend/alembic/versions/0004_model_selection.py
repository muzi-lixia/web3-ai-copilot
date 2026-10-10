"""每对话模型选择，运行时覆盖不影响已开始的轮次。"""

from alembic import op

revision = "0004_model_selection"
down_revision = "0003_service_boundaries"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent_conversations ADD COLUMN model_provider text")


def downgrade():
    op.execute("ALTER TABLE agent_conversations DROP COLUMN model_provider")
