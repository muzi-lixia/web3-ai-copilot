"""日志初始化：标准库统一格式及第三方日志降噪；当前未实现独立链路追踪。"""

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """配置标准库根日志及第三方库的输出级别。

    先替换现有处理器，防止重复初始化造成每条日志打印多次；RPC 库压到 WARNING，
    避免 DEBUG 日志输出包含节点凭据的请求地址。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx 每次请求都打一行，开发期噪音太大
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # RPC 传输层 DEBUG 日志可能包含带凭据的节点 URL，生产与开发都限制输出。
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """返回按模块命名的日志器，继承根日志配置，便于定位异常来源。"""
    return logging.getLogger(name)
