"""对话产品数据仓储。模型历史由框架 Checkpointer 保存，这里只管理归属和展示轮次。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import trace_id


class ConversationRepository:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    async def conversation(self, user):
        retired = None
        async with self.db.transaction(user) as s:
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
            owner = await s.scalar(
                text("SELECT id FROM agent_conversations WHERE id=:c AND user_id=:u FOR UPDATE"),
                dict(c=cid, u=user),
            )
            if not owner:
                raise NotFoundError("会话不存在")
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
            if await s.scalar(
                text("SELECT id FROM agent_turns WHERE conversation_id=:c AND state IN ('queued','running')"),
                dict(c=cid),
            ):
                raise ConflictError("当前对话已有执行中的轮次")
            tid = str(uuid4())
            await s.execute(
                text("""INSERT INTO agent_turns
                (id,conversation_id,user_id,client_id,query,display_text,trace_id)
                VALUES(:id,:c,:u,:i,:q,:d,:t)"""),
                dict(id=tid, c=cid, u=user, i=client_id, q=query, d=display_text, t=trace_id.get()),
            )
            return tid, True

    async def read(self, user, tid):
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
        async with self.db.transaction(user) as s:
            rank = await s.scalar(
                text(
                    "SELECT count(*) FROM agent_turns WHERE user_id=:u "
                    "AND conversation_id=:c AND (created_at,id)<=(:at,:id)"
                ),
                dict(u=user, c=row["conversation_id"], at=row["created_at"], id=row["id"]),
            )
        value["seq"] = rank * 2
        return value

    async def update(self, user, tid, answer, state, results, *, error=None, usage=None):
        async with self.db.transaction(user) as s:
            await s.execute(
                text("""UPDATE agent_turns SET answer=:a,state=:s,
                results=CAST(:r AS jsonb),error_code=:e,usage=CAST(:g AS jsonb),version=version+1
                WHERE id=:t AND user_id=:u"""),
                dict(
                    a=answer,
                    s=state,
                    r=json.dumps(results),
                    e=error,
                    g=json.dumps(usage or {}),
                    t=tid,
                    u=user,
                ),
            )

    async def add_ref(self, user, cid, result_id):
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
        async with self.db.transaction(user) as s:
            for rid in set(ids):
                if not await s.scalar(
                    text("""SELECT result_id FROM agent_result_refs
                    WHERE result_id=:r AND user_id=:u AND conversation_id=:c"""),
                    dict(r=rid, u=user, c=cid),
                ):
                    raise NotFoundError("结果引用不属于当前对话或已经失效")

    async def history(self, user, before=None, limit=50):
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
                ),
                self.snapshot(dict(row), seq + 1)["message"],
            ]
            if row["state"] in ("queued", "running"):
                active = row["id"]
        items = [m for m in items if before is None or m["seq"] < before]
        selected = items[-limit:]
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
            ),
            error=row["error_code"],
            context_info={"execution_phase": state},
        )

    async def clear(self, user):
        async with self.db.transaction(user) as s:
            await s.execute(text("DELETE FROM agent_conversations WHERE user_id=:u"), dict(u=user))

    async def model_provider(self, user, cid):
        async with self.db.transaction(user) as s:
            return await s.scalar(
                text("SELECT model_provider FROM agent_conversations WHERE id=:c AND user_id=:u"),
                dict(c=cid, u=user),
            )

    async def set_model(self, user, cid, provider):
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
        async with self.db.transaction(user) as s:
            await s.execute(
                text("DELETE FROM agent_conversations WHERE user_id=:u AND id=:c AND expires_at<now()"),
                dict(u=user, c=cid),
            )

    async def refs(self, user, cid):
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
