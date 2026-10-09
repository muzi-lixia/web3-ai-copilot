"""应用装配根。只在这里创建资源及连接依赖，业务层不导入此模块。"""

from dataclasses import dataclass

from app.ai.conversation.repository import ChatRepository
from app.ai.conversation.service import ChatRuntime
from app.ai.llm.ollama import OllamaClient
from app.ai.llm.protocol import ChatModel
from app.ai.memory.service import ConversationMemory
from app.core.config import Settings
from app.infrastructure.database.runtime_lock import RuntimeLock
from app.infrastructure.database.session import Database
from app.modules.auth.service import AuthService


@dataclass
class ApplicationServices:
    """应用级依赖容器，同时记录资源的所有权。

    repository、memory 和 chat 共享当前容器的数据库与模型适配器。
    这里只负责组装与销毁；API、领域服务和 AI 策略不能反向依赖容器。
    """

    settings: Settings
    database: Database
    auth: AuthService
    repository: ChatRepository
    model: ChatModel
    memory: ConversationMemory
    chat: ChatRuntime

    @classmethod
    def build(cls, settings: Settings, model: ChatModel | None = None) -> "ApplicationServices":
        """按数据库、仓储、模型、记忆策略和任务运行时的顺序组装依赖。

        传入 ChatModel 可替换真实 Ollama，例如测试使用受控流。创建引擎不等于
        取得运行锁，真正的启动检查由 ChatRuntime.start 在应用生命周期内完成。
        """
        # 组装只连接依赖关系；聊天启动取得租约后才允许对历史执行恢复写入。
        database = Database(settings.database_url)
        # 封装所有聊天数据的持久化操作 （持久化数据）
        repository = ChatRepository(database, settings)
        # 创建 AI 模型客户端
        model = model if model is not None else OllamaClient(settings)
        # 创建会话记忆
        memory = ConversationMemory(repository, model, settings)
        # 创建会话运行时 RuntimeLock: PostgreSQL 单实例锁（确保只有一个进程处理聊天）
        chat = ChatRuntime(repository, memory, model, RuntimeLock(database.engine), settings)
        return cls(settings, database, AuthService(settings), repository, model, memory, chat)

    async def close(self) -> None:
        # 即使聊天收尾失败，也必须关闭连接池；资源所有权仅属于容器。
        """按资源依赖顺序关闭：先收尾聊天，再销毁数据库连接池。

        聊天收尾需要数据库，不能提前关闭连接池；即使收尾失败也必须释放池资源。
        """
        try:
            await self.chat.stop()
        finally:
            await self.database.close()
