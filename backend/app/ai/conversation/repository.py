"""Copilot 数据访问层：显式用户过滤 + 数据库 RLS 双重隔离。

所有入口接收经过鉴权的 owner，使用 transaction 创建短事务；不接收前端自报的身份。
此层负责消息顺序、幂等约束和原子写入，不调用模型、不管理 HTTP 连接。
查不到和不属于当前用户统一返回 404，避免暴露其他用户资源的存在性。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.ai.conversation.models import ChatMessage, ChatSession, ChatSummary, ChatTurn
from app.ai.conversation.schemas import SummaryState
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.infrastructure.database.session import Database


def message_data(row: ChatMessage) -> dict:
    """返回稳定的消息 ID、顺序与状态；前端据此替换快照，而不是重复追加正文。"""
    return {
        "id": str(row.id),
        "turn_id": str(row.turn_id),
        "seq": row.seq,
        "role": row.role,
        "content": row.content,
        "status": row.status,
    }


async def require_session(db: AsyncSession, owner: str, session_id: UUID, lock: bool = False) -> ChatSession:
    """验证会话归属，必要时加行锁；用于串行分配消息序号和修改会话。"""
    query = select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_address == owner.lower())
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if row is None:
        raise NotFoundError("会话不存在")
    return row


class ChatRepository:
    """只负责持久化事务；数据库与配置由装配层注入，HTTP 与调度不进入此层。"""

    def __init__(self, database: Database, settings: Settings) -> None:
        """绑定当前应用数据库和配置；所有用户操作在方法内部开启独立短事务。"""
        self.database = database
        self.settings = settings

    async def recover_interrupted(self) -> None:
        """持运行锁后执行受限维护函数，收尾上一次进程遗留的 running。"""
        async with self.database.engine.begin() as connection:
            await connection.execute(text("SELECT chat_recover_interrupted()"))

    async def find_session(self, owner: str) -> dict[str, str] | None:
        """只读取当前固定会话，不通过 GET 隐式创建；选择规则与创建入口一致。"""
        async with self.database.transaction(owner, snapshot=True) as db:
            row = await db.scalar(
                select(ChatSession)
                .where(ChatSession.user_address == owner.lower())
                .order_by(
                    (ChatSession.next_seq > 1).desc(), ChatSession.created_at.desc(), ChatSession.id.desc()
                )
                .limit(1)
            )
            return {"id": str(row.id)} if row else None

    async def get_or_create_session(self, owner: str) -> dict:
        """固定会话入口，调用方需持用户锁；旧多会话数据只选一个继续使用，不删除旧历史。

        优先选最新创建的非空会话；此排序不随改名和回答变化，后续每次打开返回同一 ID。
        对外已移除新建多会话入口，单实例用户锁保证首次并发打开不会重复创建。
        """
        async with self.database.transaction(owner) as db:
            row = await db.scalar(
                select(ChatSession)
                .where(ChatSession.user_address == owner.lower())
                .order_by(
                    (ChatSession.next_seq > 1).desc(), ChatSession.created_at.desc(), ChatSession.id.desc()
                )
                .limit(1)
            )
            created = row is None
            if row is None:
                row = ChatSession(user_address=owner.lower())
                db.add(row)
                await db.flush()
            # 单会话页面只需要关联 ID，不再返回旧会话列表的标题与时间字段。
            return {"id": str(row.id), "created": created}

    async def messages(
        self, owner: str, session_id: UUID, before_seq: int | None = None, limit: int = 50
    ) -> dict:
        """倒序读取一页，再转成时间正序展示；同时返回活动轮次和最新摘要版本。"""
        async with self.database.transaction(owner, snapshot=True) as db:
            await require_session(db, owner, session_id)
            query = select(ChatMessage).where(
                ChatMessage.session_id == session_id, ChatMessage.user_address == owner.lower()
            )
            if before_seq is not None:
                query = query.where(ChatMessage.seq < before_seq)
            # 多取一条判断是否有更早页；返回时逆序成正序，分页过程不会重新生成消息。
            rows = list((await db.scalars(query.order_by(ChatMessage.seq.desc()).limit(limit + 1))).all())
            active = await db.scalar(
                select(ChatTurn.id).where(
                    ChatTurn.session_id == session_id,
                    ChatTurn.user_address == owner.lower(),
                    ChatTurn.status == "running",
                )
            )
            summary = await db.scalar(
                select(ChatSummary)
                .where(ChatSummary.session_id == session_id, ChatSummary.user_address == owner.lower())
                .order_by(ChatSummary.version.desc())
            )
            return {
                "items": [message_data(row) for row in reversed(rows[:limit])],
                "next_cursor": rows[limit - 1].seq if len(rows) > limit else None,
                "active_turn_id": str(active) if active else None,
                "summary_version": summary.version if summary else None,
            }

    async def create_turn(
        self,
        owner: str,
        session_id: UUID,
        client_id: UUID,
        content: str | None = None,
        retry_of: UUID | None = None,
    ) -> tuple[UUID, bool]:
        """原子接受一次问题或重试，并预先建立 assistant 正文快照行。

        先锁会话，再处理幂等和活动轮次检查；相同请求只返回旧轮次，不重复调用模型。
        首次发送一起创建用户问题、轮次和回答占位；重试只增加生成尝试与回答，不复制问题。
        """
        async with self.database.transaction(owner) as db:
            # 会话行锁串行化序号分配、幂等判断及忙碌检查，释放点是整个事务结束。
            session = await require_session(db, owner, session_id, lock=True)
            existing = await db.scalar(
                select(ChatTurn).where(
                    ChatTurn.session_id == session_id,
                    ChatTurn.user_address == owner.lower(),
                    ChatTurn.client_msg_id == client_id,
                )
            )
            # 先判断幂等再判断忙碌：原请求正在执行时，重发仍应返回原 ID，而不是 409。
            if existing:
                if existing.retry_of != retry_of:
                    raise ConflictError("该消息标识已用于其他请求")
                if retry_of is None:
                    prompt = await db.scalar(
                        select(ChatMessage).where(ChatMessage.id == existing.prompt_message_id)
                    )
                    if prompt.content != content:
                        raise ConflictError("该消息标识已用于其他问题")
                return existing.id, False
            active = await db.scalar(
                select(ChatTurn.id).where(
                    ChatTurn.session_id == session_id,
                    ChatTurn.user_address == owner.lower(),
                    ChatTurn.status == "running",
                )
            )
            if active:
                raise ConflictError("该会话有正在生成的回复，请等待或停止生成")
            # 预分配关联 ID，延迟外键允许问题和轮次在同一事务互相引用。
            turn_id = uuid4()
            prompt_id = uuid4()
            retry = None
            if retry_of:
                retry = await db.scalar(
                    select(ChatTurn).where(
                        ChatTurn.id == retry_of,
                        ChatTurn.session_id == session_id,
                        ChatTurn.user_address == owner.lower(),
                    )
                )
                if retry is None:
                    raise NotFoundError("原轮次不存在")
                last = await db.scalar(
                    select(ChatMessage.turn_id)
                    .where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.user_address == owner.lower(),
                        ChatMessage.role == "assistant",
                    )
                    .order_by(ChatMessage.seq.desc())
                    .limit(1)
                )
                # 暂不允许重写较早轮次，避免成功历史分叉后还需回滚摘要及后续回答。
                if retry.status not in {"failed", "cancelled", "interrupted"} or last != retry.id:
                    raise ConflictError("只能重试当前会话最后一轮未完成的回复")
                prompt_id = retry.prompt_message_id
            turn = ChatTurn(
                id=turn_id,
                session_id=session_id,
                user_address=owner.lower(),
                client_msg_id=client_id,
                prompt_message_id=prompt_id,
                retry_of=retry_of,
                model=self.settings.copilot_model,
                context_info=retry.context_info if retry else {},
                summary_version=retry.summary_version if retry else None,
            )
            # flush 发送 INSERT 但尚不提交，后续问题和回答占位与轮次仍属于同一原子事务。
            db.add(turn)
            await db.flush()
            if retry is None:
                db.add(
                    ChatMessage(
                        id=prompt_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        user_address=owner.lower(),
                        seq=session.next_seq,
                        role="user",
                        content=content,
                        status="complete",
                    )
                )
                session.next_seq += 1
            db.add(
                ChatMessage(
                    session_id=session_id,
                    turn_id=turn_id,
                    user_address=owner.lower(),
                    seq=session.next_seq,
                    role="assistant",
                    content="",
                    status="streaming",
                )
            )
            session.next_seq += 1
            session.updated_at = datetime.now(UTC)
            return turn_id, True

    async def turn_snapshot(self, owner: str, turn_id: UUID) -> dict:
        """读取轮次和正文的已提交快照；用于刷新、重连以及运行时之外的终态查询。"""
        async with self.database.transaction(owner, snapshot=True) as db:
            turn = await db.scalar(
                select(ChatTurn).where(ChatTurn.id == turn_id, ChatTurn.user_address == owner.lower())
            )
            if turn is None:
                raise NotFoundError("轮次不存在")
            answer = await db.scalar(
                select(ChatMessage).where(
                    ChatMessage.turn_id == turn_id,
                    ChatMessage.user_address == owner.lower(),
                    ChatMessage.role == "assistant",
                )
            )
            return {
                "turn_id": str(turn.id),
                "session_id": str(turn.session_id),
                "status": turn.status,
                "version": turn.version,
                "message": message_data(answer),
                "model": turn.model,
                "error": turn.error,
                "context_info": turn.context_info,
                "summary_version": turn.summary_version,
                "usage": turn.usage,
            }

    async def checkpoint(
        self,
        owner: str,
        turn_id: UUID,
        content: str | None,
        status: str = "running",
        error: str | None = None,
        usage: dict | None = None,
        version: int | None = None,
    ) -> dict | None:
        """在同一事务中更新回答、快照版本、轮次状态和用量，避免正文已完成但轮次仍占用会话。"""
        async with self.database.transaction(owner) as db:
            turn = await db.scalar(
                select(ChatTurn)
                .where(ChatTurn.id == turn_id, ChatTurn.user_address == owner.lower())
                .with_for_update()
            )
            # 终态不可再次改写；删除或取消与迟到快照竞态时，由运行时按已结束处理。
            if turn is None or turn.status != "running":
                return None
            answer = await db.scalar(
                select(ChatMessage).where(
                    ChatMessage.turn_id == turn_id,
                    ChatMessage.user_address == owner.lower(),
                    ChatMessage.role == "assistant",
                )
            )
            answer.content = content
            answer.status = "streaming" if status == "running" else status
            turn.version = max(turn.version + 1, version or 0)
            turn.status = status
            turn.error = error
            if usage is not None:
                turn.usage = usage
            if status != "running":
                turn.finished_at = datetime.now(UTC)
            await db.flush()
            return {
                "type": "snapshot" if status == "running" else "done",
                "turn_id": str(turn.id),
                "version": turn.version,
                "status": status,
                "message": message_data(answer),
                "error": error,
                "usage": turn.usage,
            }

    async def context_data(self, owner: str, session_id: UUID) -> tuple[list[dict], SummaryState | None]:
        """按成功轮次恢复问题/回答配对，并读取最新摘要；失败或取消的半段回答不进入上下文。"""
        async with self.database.transaction(owner, snapshot=True) as db:
            await require_session(db, owner, session_id)
            prompt = aliased(ChatMessage)
            answer = aliased(ChatMessage)
            query = select(prompt, answer).join(ChatTurn, ChatTurn.prompt_message_id == prompt.id)
            query = query.join(answer, (answer.turn_id == ChatTurn.id) & (answer.role == "assistant"))
            # 通过轮次找问题而非仅按相邻 seq 配对：重试复用旧问题，回答序号可能不相邻。
            rows = (
                await db.execute(
                    query.where(
                        ChatTurn.session_id == session_id,
                        ChatTurn.user_address == owner.lower(),
                        ChatTurn.status.in_(["completed", "truncated"]),
                    ).order_by(answer.seq)
                )
            ).all()
            summary = await db.scalar(
                select(ChatSummary)
                .where(ChatSummary.session_id == session_id, ChatSummary.user_address == owner.lower())
                .order_by(ChatSummary.version.desc())
                .limit(1)
            )
            pairs = [{"seq": a.seq, "messages": [message_data(p), message_data(a)]} for p, a in rows]
            return pairs, SummaryState.model_validate(summary) if summary else None

    async def set_context(
        self, owner: str, turn_id: UUID, context: dict, summary_version: int | None
    ) -> None:
        """记录本轮实际模型输入和摘要版本；失败重试可复用原输入，避免历史变化导致上下文漂移。"""
        async with self.database.transaction(owner) as db:
            turn = await db.scalar(
                select(ChatTurn).where(ChatTurn.id == turn_id, ChatTurn.user_address == owner.lower())
            )
            if turn is None:
                raise NotFoundError("轮次不存在")
            turn.context_info = context
            turn.summary_version = summary_version

    async def prompt_for(self, owner: str, turn_id: UUID) -> str:
        """按轮次关联找到原始问题；重试不依赖前端重新提交问题文本。"""
        async with self.database.transaction(owner, snapshot=True) as db:
            turn = await db.scalar(
                select(ChatTurn).where(ChatTurn.id == turn_id, ChatTurn.user_address == owner.lower())
            )
            if turn is None:
                raise NotFoundError("轮次不存在")
            prompt = await db.scalar(
                select(ChatMessage).where(
                    ChatMessage.id == turn.prompt_message_id, ChatMessage.user_address == owner.lower()
                )
            )
            return prompt.content

    async def save_summary(
        self,
        owner: str,
        session_id: UUID,
        previous_version: int,
        upto: int,
        content: str | None,
        source_ids: list[str],
    ) -> bool:
        """锁定会话并检查旧版本，只有版本仍一致时保存新摘要，防止并发摘要覆盖较新的结果。"""
        async with self.database.transaction(owner) as db:
            await require_session(db, owner, session_id, lock=True)
            latest = await db.scalar(
                select(ChatSummary.version)
                .where(ChatSummary.session_id == session_id, ChatSummary.user_address == owner.lower())
                .order_by(ChatSummary.version.desc())
                .limit(1)
            )
            # 乐观版本检查配合会话行锁；计算期间出现更新时丢弃旧计算结果，不覆盖新摘要。
            if (latest or 0) != previous_version:
                return False
            db.add(
                ChatSummary(
                    session_id=session_id,
                    user_address=owner.lower(),
                    version=previous_version + 1,
                    covered_through_seq=upto,
                    content=content,
                    source_message_ids=source_ids,
                    model=self.settings.copilot_model,
                )
            )
            return True

    async def delete_all(self, owner: str) -> None:
        """只清空当前用户的对话数据，不删除登录凭证或其他用户记录。"""
        async with self.database.transaction(owner) as db:
            await db.execute(delete(ChatSession).where(ChatSession.user_address == owner.lower()))

    async def session_ids(self, owner: str) -> set[UUID]:
        """获取本人的会话 ID 集合，让清理运行时任务时不会误取消其他用户的摘要任务。"""
        async with self.database.transaction(owner) as db:
            return set(
                (
                    await db.scalars(select(ChatSession.id).where(ChatSession.user_address == owner.lower()))
                ).all()
            )

    async def active_turn(self, owner: str, session_id: UUID) -> UUID | None:
        """查询本人会话的活动轮次，供运行时在接受新问题前收尾无任务的孤儿记录。"""
        async with self.database.transaction(owner) as db:
            await require_session(db, owner, session_id)
            return await db.scalar(
                select(ChatTurn.id).where(
                    ChatTurn.session_id == session_id,
                    ChatTurn.user_address == owner.lower(),
                    ChatTurn.status == "running",
                )
            )
