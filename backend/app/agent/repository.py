"""对话产品数据仓储。模型历史由框架 Checkpointer 保存，这里只管理归属和展示轮次。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import trace_id


class ConversationRepository:
    """对话产品的数据仓储：只管理归属与展示轮次。

    模型历史交给框架 Checkpointer，这里不重复保存 prompt 和消息树；
    每个方法都走 db.transaction(user)，由连接层负责注入当前用户身份。
    """

    def __init__(self, database, settings):
        """持有数据库连接层与配置；仓储本身无状态，所有操作按调用传入 user 开事务。"""
        self.db, self.settings = database, settings

    async def conversation(self, user):
        """取本人固定会话，没有就建一个；过期会话在没有活动轮次时直接重建。

        会话按 user_id 唯一，因此 PUT 并发不会产生第二个固定会话。
        """
        retired = None
        async with self.db.transaction(user) as s:
            # 行锁会话行：并发 PUT 被串行化，后到的请求看到的是已建好的行。
            row = (
                (
                    await s.execute(
                        text("SELECT * FROM agent_conversations WHERE user_id=:u FOR UPDATE"), dict(u=user)
                    )
                )
                .mappings()
                .first()
            )
            if row and row["expires_at"] <= datetime.now(UTC):
                # 会话已过期但仍有轮次在跑时不能重建，否则会丢掉正在进行的生成。
                active = await s.scalar(
                    text(
                        "SELECT count(*) FROM agent_turns WHERE conversation_id=:c "
                        "AND state IN ('queued','running')"
                    ),
                    dict(c=row["id"]),
                )
                if active:
                    raise ConflictError("会话仍在执行，请稍后重置")
                await s.execute(text("DELETE FROM agent_conversations WHERE user_id=:u"), dict(u=user))
                row = None
            if row:
                return dict(id=row["id"], created=False)
            cid = str(uuid4())
            expires = datetime.now(UTC) + timedelta(hours=self.settings.conversation_ttl_hours)
            # 冲突时返回已有 ID，PUT 并发不产生第二个固定会话。
            saved = await s.scalar(
                text("""INSERT INTO agent_conversations(id,user_id,expires_at)
                VALUES(:c,:u,:e) ON CONFLICT(user_id) DO UPDATE SET user_id=excluded.user_id RETURNING id"""),
                dict(c=cid, u=user, e=expires),
            )
            return dict(id=saved, created=saved == cid, expired_thread_id=retired)

    async def create_turn(self, user, cid, client_id, query, display_text=None):
        """原文仅供本人展示；query 为模型安全输入，两者独立保存与校验幂等性。"""
        display_text = query if display_text is None else display_text
        async with self.db.transaction(user) as s:
            # 锁住会话行：把「查重 + 建轮次」串行化，并发的同 client_id 请求只有一个能通过。
            owner = await s.scalar(
                text("SELECT id FROM agent_conversations WHERE id=:c AND user_id=:u FOR UPDATE"),
                dict(c=cid, u=user),
            )
            if not owner:
                raise NotFoundError("会话不存在")
            # 幂等命中：同一个 client_id 必须对应同一份问题，否则是客户端复用了标识。
            existing = (
                await s.execute(
                    text(
                        "SELECT id,query,display_text FROM agent_turns "
                        "WHERE conversation_id=:c AND client_id=:i"
                    ),
                    dict(c=cid, i=client_id),
                )
            ).first()
            if existing:
                if (
                    existing.query != query
                    or (existing.display_text if existing.display_text is not None else existing.query)
                    != display_text
                ):
                    raise ConflictError("幂等标识已用于其他消息")
                return existing.id, False
            # 一个会话同时只允许一个未完成轮次（数据库另有偏索引兜底）。
            if await s.scalar(
                text("SELECT id FROM agent_turns WHERE conversation_id=:c AND state IN ('queued','running')"),
                dict(c=cid),
            ):
                raise ConflictError("当前对话已有执行中的轮次")
            tid = str(uuid4())
            # state 省略，取列默认值（排队态）；生成任务拿到模型额度后才推进到 running。
            await s.execute(
                text("""INSERT INTO agent_turns
                (id,conversation_id,user_id,client_id,query,display_text,trace_id)
                VALUES(:id,:c,:u,:i,:q,:d,:t)"""),
                dict(id=tid, c=cid, u=user, i=client_id, q=query, d=display_text, t=trace_id.get()),
            )
            return tid, True

    async def read(self, user, tid):
        """读取一轮的完整状态，并补上会话内展示序号 seq。

        seq 是这一轮在会话里排第几位（每轮占用户与回答两个位置），
        和轮次内的 version 是两回事：前者用于排序，后者用于判断快照新旧。
        """
        async with self.db.transaction(user) as s:
            row = (
                (
                    await s.execute(
                        text("SELECT * FROM agent_turns WHERE id=:t AND user_id=:u"), dict(t=tid, u=user)
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise NotFoundError("轮次不存在")
        value = dict(row)
        # seq 要按会话内全部轮次排名才得出，所以要单独查一次；(created_at,id) 元组比较
        # 让同一时刻创建的两轮也有稳定次序，不依赖单列时间戳。
        async with self.db.transaction(user) as s:
            rank = await s.scalar(
                text(
                    "SELECT count(*) FROM agent_turns WHERE user_id=:u "
                    "AND conversation_id=:c AND (created_at,id)<=(:at,:id)"
                ),
                dict(u=user, c=row["conversation_id"], at=row["created_at"], id=row["id"]),
            )
        # 每轮占两个展示位（用户提问 + 助手回答），故 seq = 排名 * 2。
        value["seq"] = rank * 2
        return value

    async def update(self, user, tid, answer, state, results, *, error=None, usage=None, tool_trace=None):
        """写入一轮的展示状态；answer 是全量正文，不是增量。

        version 每次自增，订阅端据此判断快照是否变新 —— 所以这里必须整体覆盖，
        不能只写新增片段，否则断线重连会拼出互相矛盾的正文。
        tool_trace 传 None 表示不改动该列：只有生成过程会更新轨迹，
        取消、补写终态等路径回写时应保留已记录的内容。
        """
        async with self.db.transaction(user) as s:
            # version 由数据库自增，是订阅端判断「快照是否变新」的唯一依据。
            # answer 整体覆盖而非追加：任何时刻读到的都是一份自洽的完整正文。
            await s.execute(
                text("""UPDATE agent_turns SET answer=:a,state=:s,
                results=CAST(:r AS jsonb),error_code=:e,usage=CAST(:g AS jsonb),
                tool_trace=coalesce(CAST(:c AS jsonb),tool_trace),version=version+1
                WHERE id=:t AND user_id=:u"""),
                dict(
                    a=answer,
                    s=state,
                    r=json.dumps(results),
                    e=error,
                    g=json.dumps(usage or {}),
                    c=None if tool_trace is None else json.dumps(tool_trace),
                    t=tid,
                    u=user,
                ),
            )

    async def add_ref(self, user, cid, result_id):
        """把基础服务产出的结果引用登记到当前会话，供后续汇总和清理使用。"""
        async with self.db.transaction(user) as s:
            # 校验会话仍存在，删除中的工具不能登记到新会话。
            if not await s.scalar(
                text("SELECT id FROM agent_conversations WHERE id=:c AND user_id=:u"), dict(c=cid, u=user)
            ):
                raise NotFoundError("会话不存在")
            await s.execute(
                text("""INSERT INTO agent_result_refs(result_id,user_id,conversation_id)
                VALUES(:r,:u,:c) ON CONFLICT DO NOTHING"""),
                dict(r=result_id, u=user, c=cid),
            )

    async def check_refs(self, user, cid, ids):
        """确认这些结果引用确实属于当前用户当前会话，否则拒绝汇总。

        这是汇总工具的越权闸门：模型能编造 result_id，但编不出归属关系。
        """
        async with self.db.transaction(user) as s:
            # 逐条校验；只要有一条不属于本会话就整体拒绝，不给「部分汇总」的机会。
            for rid in set(ids):
                if not await s.scalar(
                    text("""SELECT result_id FROM agent_result_refs
                    WHERE result_id=:r AND user_id=:u AND conversation_id=:c"""),
                    dict(r=rid, u=user, c=cid),
                ):
                    raise NotFoundError("结果引用不属于当前对话或已经失效")

    async def history(self, user, before=None, limit=50):
        """按会话内顺序读取消息页，并附带仍在执行中的活动轮次 id。

        用户消息取 display_text（原文），回答取快照正文；因为正文快照始终是全量，
        前端可以按消息 id 覆盖，而不是把两段拼起来。
        """
        async with self.db.transaction(user) as s:
            rows = (
                (
                    await s.execute(
                        text("""SELECT t.* FROM agent_turns t JOIN agent_conversations c
                ON c.id=t.conversation_id WHERE t.user_id=:u AND c.expires_at>now()
                ORDER BY t.created_at,t.id"""),
                        dict(u=user),
                    )
                )
                .mappings()
                .all()
            )
        items = []
        active = None
        for index, row in enumerate(rows):
            # 用户提问占奇数位、回答占偶数位，保证同一轮的两条消息相邻且顺序稳定。
            seq = index * 2 + 1
            items += [
                dict(
                    id=row["id"] + "-user",
                    turn_id=row["id"],
                    seq=seq or row.get("seq", 0),
                    role="user",
                    # 迁移前未保存原文，只能显示既有安全文本，不能猜测恢复。
                    content=row["display_text"] if row.get("display_text") is not None else row["query"],
                    status="complete",
                    created_at=row["created_at"].isoformat(),
                ),
                self.snapshot(dict(row), seq + 1)["message"],
            ]
            if row["state"] in ("queued", "running"):
                # 只记最后一个活动轮次：设计上一个会话最多只有一个未完成轮次。
                active = row["id"]
        items = [m for m in items if before is None or m["seq"] < before]
        selected = items[-limit:]
        # 游标取「本页第一条的 seq」，下一页拿它做上界；只有真的翻页了才给游标。
        return dict(
            items=selected,
            next_cursor=selected[0]["seq"] if len(items) > limit else None,
            active_turn_id=active,
        )

    @staticmethod
    def snapshot(row, seq=0):
        """展示快照只含结果引用，真实结果只能经基础服务鉴权读取。"""
        state = row["state"]
        return dict(
            turn_id=row["id"],
            session_id=row["conversation_id"],
            version=row["version"],
            status=state,
            message=dict(
                id=row["id"] + "-assistant",
                turn_id=row["id"],
                seq=seq or row.get("seq", 0),
                role="assistant",
                content=row["answer"],
                status=state,
                result_refs=row["results"],
                # 提问时刻，与同轮的用户消息共用；回答完成时刻另行记录。
                created_at=row["created_at"].isoformat(),
            ),
            error=row["error_code"],
            # execution_phase 直接复用 DB state，目前只可能是 queued/running/completed/failed/cancelled。
            context_info={"execution_phase": state},
        )

    async def clear(self, user):
        """删除本人会话；消息、轮次与结果引用靠外键级联一起清掉。"""
        async with self.db.transaction(user) as s:
            await s.execute(text("DELETE FROM agent_conversations WHERE user_id=:u"), dict(u=user))

    async def model_provider(self, user, cid):
        """读取该会话选择的模型提供方；为空表示沿用部署默认。"""
        async with self.db.transaction(user) as s:
            return await s.scalar(
                text("SELECT model_provider FROM agent_conversations WHERE id=:c AND user_id=:u"),
                dict(c=cid, u=user),
            )

    async def set_model(self, user, cid, provider):
        """切换会话模型；有轮次在执行时拒绝，避免同一轮中途换模型。"""
        async with self.db.transaction(user) as s:
            if await s.scalar(
                text("SELECT id FROM agent_turns WHERE conversation_id=:c AND state IN ('queued','running')"),
                dict(c=cid),
            ):
                raise ConflictError("请等待当前轮次结束后切换模型")
            await s.execute(
                text("UPDATE agent_conversations SET model_provider=:p WHERE id=:c AND user_id=:u"),
                dict(p=provider, c=cid, u=user),
            )

    async def find_client_turn(self, user, cid, client_id, query, display_text=None):
        """按幂等键回查已有轮次。

        同一个 client_id 必须对应同一份问题；否则说明客户端复用了标识，
        这时宁可报冲突，也不能把两段不同的问题当成一次重发。
        """
        display_text = query if display_text is None else display_text
        async with self.db.transaction(user) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT id,query,display_text FROM agent_turns WHERE conversation_id=:c "
                        "AND user_id=:u AND client_id=:i"
                    ),
                    dict(c=cid, u=user, i=client_id),
                )
            ).first()
        if row and (
            row.query != query
            or (row.display_text if row.display_text is not None else row.query) != display_text
        ):
            raise ConflictError("幂等标识已用于其他问题")
        return row.id if row else None

    async def clear_expired(self, user, cid):
        """清理已过期的会话；调用方需先删除框架 checkpoint，再删这里的行。"""
        async with self.db.transaction(user) as s:
            await s.execute(
                text("DELETE FROM agent_conversations WHERE user_id=:u AND id=:c AND expires_at<now()"),
                dict(u=user, c=cid),
            )

    async def refs(self, user, cid):
        """列出会话内的全部结果引用，供清空对话时逐个删除真实结果。"""
        async with self.db.transaction(user) as s:
            return list(
                (
                    await s.scalars(
                        text(
                            "SELECT result_id FROM agent_result_refs WHERE user_id=:u AND conversation_id=:c"
                        ),
                        dict(u=user, c=cid),
                    )
                ).all()
            )
