"""离线链路的失败分类：错误码供日志聚合与运维分流，消息给看到失败原因的人。"""

from enum import StrEnum


class FailureKind(StrEnum):
    """失败该由谁解决：文件侧的问题重试无用，服务侧的问题重试有意义。"""

    INPUT = "input"
    SYSTEM = "system"


class IngestionError(Exception):
    """离线链路某一步的失败，带阶段、分类与可聚合的错误码。

    阶段取 load / clean / split / embed，错误码取「阶段.原因」。
    """

    def __init__(self, *, stage: str, kind: FailureKind, code: str, message: str) -> None:
        """保存分类信息；消息面向人，日志只取阶段、错误码与分类。"""
        super().__init__(message)
        self.stage = stage
        self.kind = kind
        self.code = code
