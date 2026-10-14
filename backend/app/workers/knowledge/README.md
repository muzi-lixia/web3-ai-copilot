# 知识库文档 worker

独立后台进程，不提供 HTTP 接口，不加载基础服务或 Agent 应用。

在 backend 目录启动：

```bash
uv run python -m app.workers.knowledge
```

默认读取 backend/.env；独立部署可用单独配置：

```bash
cp .env.worker.example .env.worker.local
uv run python -m app.workers.knowledge --env-file .env.worker.local
```

- `__main__.py`：命令行入口和配置文件选择。
- `main.py`：进程生命周期、信号处理、轮询和数据库连接池。
- `config.py`：独立 WorkerSettings，只校验 worker 配置。
- `consumer.py`：任务领取、续租、文档处理和提交。
- `pipeline.py`：离线处理编排与分片向量化。
- `loader.py`：PDF、DOCX、Markdown、TXT 解析。
- `cleaner.py`、`splitter.py`：清洗与结构切分。
- `errors.py`：离线处理阶段与失败分类。

离线处理代码全部位于此包。在线文档管理、检索服务，以及两边共享的数据库仓储、原件路径、配置和向量版本约定保留在 `app/modules/knowledge`；embedding 客户端位于 `app/infrastructure/providers/embedding.py`，由单向依赖复用。HTTP 服务不依赖 worker 包；worker 不依赖 HTTP 或 Agent。

它仍使用同仓库、同 Python 依赖环境与数据库队列，不需要复制代码或新建仓库。部署时可作为独立进程或容器启动。原件存储和 embedding 配置必须与 HTTP 服务一致，数据库须已应用最新迁移。SIGTERM/SIGINT 取消本次处理并关闭数据库连接；遗留任务由租约过期恢复。

worker 只使用 KNOWLEDGE_DATABASE_URL（app_knowledge 专用角色），不读取基础服务数据库连接。该角色只允许知识库处理所需表和列；初始化与授权通过 scripts/init_chat_db.py 完成。KNOWLEDGE_MAX_ATTEMPTS 默认 3，租约反复中断达到预算后停止自动重试。

每 10 秒独立更新数据库心跳并记录队列统计。无引用文件清理与处理任务都由消费者执行，磁盘错误保留清理任务。在线与入库向量化可分别配置 EMBEDDING_QUERY_BASE_URL 和 EMBEDDING_INGEST_BASE_URL，但两端模型、修订、维度须一致。


首次启动前需在 backend 目录执行 `uv run python scripts/init_chat_db.py`，初始化 app_knowledge
专用账号及权限；只执行 Alembic 迁移不会创建数据库登录角色。若报认证失败，检查
KNOWLEDGE_DATABASE_URL 中的口令与初始化时的 KNOWLEDGE_DB_PASSWORD 是否一致。
不要通过改用 Foundation 或管理员账号绕过权限初始化。
