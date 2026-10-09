"""Copilot 的四张业务表：会话、生成轮次、消息、摘要。

会话是权限与删除的边界；轮次记录一次生成尝试；消息保存展示正文；摘要压缩模型输入。
子表冗余 user_address 并使用复合外键，防止关联关系与 RLS 所依据的归属字段不一致。
所有原始消息随会话保留，摘要和快照更新不会删除原文。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class ChatSession(Base):
    """一个用户的一段独立对话，也是消息、轮次和摘要的级联删除根。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        # 子表用 (session_id, user_address) 复合外键，同时保证会话 ID 与用户归属一致。
        UniqueConstraint("id", "user_address"),
        CheckConstraint("user_address = lower(user_address)"),
        Index("chat_sessions_owner_updated", "user_address", "updated_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_address: Mapped[str] = mapped_column(String(42))
    # 旧会话管理字段保留用于已有数据兼容；单会话接口不再生成或展示标题。
    title: Mapped[str] = mapped_column(String(80), default="新对话")
    # 会话内消息序号的分配器；Repository 先锁会话行，再递增，避免并发序号冲突。
    next_seq: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatTurn(Base):
    """一次生成尝试；失败重试会创建新轮次，但可以复用同一条用户问题。"""

    __tablename__ = "chat_turns"
    __table_args__ = (
        UniqueConstraint("id", "session_id", "user_address"),
        # 同一问题的网络重发不能产生第二次生成；主动重试则使用新的 client_msg_id。
        UniqueConstraint("session_id", "client_msg_id"),
        ForeignKeyConstraint(
            ["session_id", "user_address"],
            ["chat_sessions.id", "chat_sessions.user_address"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["prompt_message_id", "session_id", "user_address"],
            ["chat_messages.id", "chat_messages.session_id", "chat_messages.user_address"],
            # 轮次先指向预分配的问题 UUID，随后同事务插入问题；外键延迟到提交时检查。
            # 两表互相引用，因此建表时单独添加此约束，解除 DDL 创建顺序的循环依赖。
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="chat_turn_prompt_fk",
        ),
        ForeignKeyConstraint(
            ["retry_of", "session_id", "user_address"],
            ["chat_turns.id", "chat_turns.session_id", "chat_turns.user_address"],
            deferrable=True,
            initially="DEFERRED",
            name="chat_turn_retry_fk",
        ),
        CheckConstraint("status IN ('running','completed','truncated','failed','cancelled','interrupted')"),
        # 数据库兜底：即使未来漏了应用层忙碌检查，同一会话也不能并行生成两个回答。
        Index("chat_one_active_turn", "session_id", unique=True, postgresql_where=text("status='running'")),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(Uuid)
    user_address: Mapped[str] = mapped_column(String(42))
    # 客户端请求幂等键：网络重发不变，主动新问题或主动重试更换 UUID。
    client_msg_id: Mapped[UUID] = mapped_column(Uuid)
    # 指向真实问题消息；重试可以指向原尝试的同一条问题。
    prompt_message_id: Mapped[UUID] = mapped_column(Uuid)
    # 关联被重试的尝试；复合外键保证不会跨用户、跨会话关联。
    retry_of: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(20), default="running")
    model: Mapped[str] = mapped_column(String(100))
    # 正文快照的版本，用于前端拒绝旧快照；它不是消息排序的 seq，也不是摘要版本。
    version: Mapped[int] = mapped_column(Integer, default=0)
    summary_version: Mapped[int | None] = mapped_column(Integer)
    # 保存实际模型输入、原文出处与裁剪原因；API 返回前会过滤完整 model_messages。
    context_info: Mapped[dict] = mapped_column(JSON, default=dict)
    # 模型实际 token 用量；与输入预算的估算值分别保存，便于校准。
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(Base):
    """可展示的原始消息；assistant 行同时承担生成过程中的持久化正文快照。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("id", "session_id", "user_address"),
        UniqueConstraint("session_id", "seq"),
        # 每次尝试最多一条用户问题和一条 assistant 回答；重试复用旧问题，不新增用户行。
        UniqueConstraint("turn_id", "role"),
        ForeignKeyConstraint(
            ["turn_id", "session_id", "user_address"],
            ["chat_turns.id", "chat_turns.session_id", "chat_turns.user_address"],
            ondelete="CASCADE",
        ),
        CheckConstraint("role IN ('user','assistant')"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(Uuid)
    turn_id: Mapped[UUID] = mapped_column(Uuid)
    user_address: Mapped[str] = mapped_column(String(42))
    # 会话内单调序号，分页与摘要覆盖使用此值，不用时间戳判断先后。
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    # assistant 占位正文从空字符串开始，快照更新覆盖同一行，而不是每个 token 插入一行。
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatSummary(Base):
    """不可变的摘要版本；覆盖范围与原文消息 ID 共同支持追溯和后续重建。"""

    __tablename__ = "chat_summaries"
    __table_args__ = (
        UniqueConstraint("session_id", "version"),
        ForeignKeyConstraint(
            ["session_id", "user_address"],
            ["chat_sessions.id", "chat_sessions.user_address"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(Uuid)
    user_address: Mapped[str] = mapped_column(String(42))
    version: Mapped[int] = mapped_column(Integer)
    # 摘要已覆盖的最后一条回答序号；组装上下文只继续读取该位置之后的成功轮次。
    covered_through_seq: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    # 摘要输入原文的消息 ID 集合；结构化条目中的 sources 必须从这个范围引用。
    source_message_ids: Mapped[list] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(30), default="summary.v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
