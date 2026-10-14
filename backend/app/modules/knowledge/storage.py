"""HTTP 与 worker 共享的原件路径约定。"""

from pathlib import Path


def document_path(settings, document_id: str, suffix: str) -> Path:
    """两种进程必须配置同一持久化目录；跨主机时由部署挂载共享存储。"""
    return Path(settings.knowledge_storage_dir) / f"{document_id}{suffix}"
