"""独立进程使用的文档消费者：解析、向量化、租约续期与结果提交。"""

import asyncio
from pathlib import Path
from uuid import uuid4

from app.core.logging import get_logger, log_context, span, trace_id
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.storage import document_path
from app.workers.knowledge.errors import FailureKind, IngestionError
from app.workers.knowledge.pipeline import KnowledgePipeline

logger = get_logger(__name__)


class KnowledgeConsumer:
    """仅由知识库 worker 装配；HTTP 服务不导入或启动消费者。"""

    def __init__(self, database, settings):
        self.worker_id = uuid4().hex
        self.settings = settings
        self.repository = KnowledgeRepository(database)
        self.pipeline = KnowledgePipeline(settings)

    async def run_pending(self) -> bool:
        """处理一条待处理文档，返回本轮是否还有事可做。

        轮询循环靠返回值决定接着取还是歇一下。单篇失败只写进该文档的原因并记一条日志，
        不向外抛：一篇坏文件不该让整条队列停住。
        """
        await self.cleanup_files()
        await self.recover()
        claim_token = uuid4().hex
        document = await self.repository.claim(
            self.worker_id, claim_token, self.settings.knowledge_task_lease_seconds
        )
        if document is None:
            return False
        token = trace_id.set(document.get("trace_id") or uuid4().hex)
        try:
            with log_context(document_id=document["id"], worker_id=self.worker_id,
                             attempt_id=uuid4().hex, attempt_count=document.get("attempt_count", 1)):
                logger.info("knowledge.claimed")
                with span("knowledge.task") as outcome:
                    processing = asyncio.create_task(self._process(document, claim_token))
                    heartbeat = asyncio.create_task(self._heartbeat(document["id"], claim_token))
                    try:
                        done, _ = await asyncio.wait(
                            (processing, heartbeat), return_when=asyncio.FIRST_COMPLETED
                        )
                        if processing in done:
                            result = await processing
                            outcome["status"] = result
                        else:
                            # 续租失败或数据库不可用：停止本次执行，保留任务待租约过期回收。
                            outcome.update(status="cancelled", error_code="lease_lost")
                            await heartbeat
                        return True
                    finally:
                        for task in (processing, heartbeat):
                            task.cancel()
                        await asyncio.gather(processing, heartbeat, return_exceptions=True)
        finally:
            trace_id.reset(token)

    async def _heartbeat(self, document_id: str, claim_token: str) -> None:
        """每三分之一租期续租一次；失去所有权即返回，由调用方取消处理。"""
        seconds = self.settings.knowledge_task_lease_seconds
        while True:
            await asyncio.sleep(seconds / 3)
            if not await self.repository.renew(document_id, claim_token, seconds):
                logger.warning("knowledge.lease_lost", extra={"document_id": document_id})
                return

    async def _process(self, document: dict, claim_token: str) -> str:
        """处理文件并携带本次领取 token 提交，任何过期结果均由仓储拒绝。"""
        suffix = Path(document["filename"]).suffix.lower()
        try:
            content = await asyncio.to_thread(self._path(document["id"], suffix).read_bytes)
        except OSError:
            await self._failed(
                document["id"],
                claim_token,
                IngestionError(
                    stage="load",
                    kind=FailureKind.INPUT,
                    code="load.read_failed",
                    message="读取原件失败，请检查共享存储",
                ),
            )
            return "failed"
        try:
            chunks = await self.pipeline.process(suffix, content)
        except IngestionError as error:
            await self._failed(document["id"], claim_token, error)
            return "failed"
        except Exception:
            logger.exception("knowledge.unexpected")
            # 没被包装过的异常说明是未预料的缺陷，按服务侧记，同样不中断队列。
            await self._failed(
                document["id"],
                claim_token,
                IngestionError(
                    stage="unknown",
                    kind=FailureKind.SYSTEM,
                    code="unexpected",
                    message="文档处理发生内部错误，请联系管理员",
                ),
            )
            return "failed"
        with span("knowledge.stage", stage="commit"):
            accepted = await self.repository.complete(document["id"], claim_token, chunks)
        if not accepted:
            logger.warning("knowledge.lease_lost", extra={"document_id": document["id"]})
            return "stale"
        logger.info("knowledge.completed", extra={"result_count": len(chunks)})
        return "success"

    async def recover(self) -> int:
        """回收已过期的执行租约，不触碰其他消费者正在处理的任务。"""
        recovered = await self.repository.retry_stuck(
            self.settings.knowledge_max_attempts, self.settings.knowledge_retry_delay_seconds
        )
        if recovered:
            logger.info("knowledge.recovered", extra={"recovered_count": recovered})
        return recovered

    async def cleanup_files(self):
        """清理无文档引用的原件与临时文件；失败记录保留并退避重试。"""
        for job in await self.repository.cleanup_candidates():
            async with self.repository.cleanup_guard(job["document_id"]) as attempt:
                if attempt is None:
                    continue
                path = self._path(job["document_id"], attempt.suffix)
                try:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                    await asyncio.to_thread(path.with_suffix(path.suffix + ".upload").unlink, missing_ok=True)
                except OSError:
                    attempt.failed = True
                    logger.exception("knowledge.cleanup_failed", extra={"document_id": job["document_id"]})

    async def _failed(self, document_id: str, claim_token: str, failure: IngestionError) -> None:
        """写失败记录并记一条日志；日志只带受控枚举，文件名与正文都不进去。"""
        accepted = await self.repository.fail(
            document_id, claim_token, str(failure), stage=failure.stage, error_code=failure.code
        )
        if not accepted:
            return
        logger.warning(
            "knowledge.failed",
            extra={
                "document_id": document_id,
                "stage": failure.stage,
                "error_code": failure.code,
                "kind": failure.kind,
            },
        )

    def _path(self, document_id: str, suffix: str) -> Path:
        return document_path(self.settings, document_id, suffix)
