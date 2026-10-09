"""并发读取合并：同一事件循环、同一函数、同一参数只执行一份在途任务。

调用方取消通过 shield 隔离，任务完成即删除登记；它不是 TTL 缓存或跨进程协调器。
"""

import asyncio
from functools import wraps

_inflight: dict[tuple, asyncio.Task] = {}


def singleflight(fn):
    """合并同时进行的相同读取，降低重复 RPC 和行情请求。

    键包含事件循环、被装饰函数及全部参数；参数必须可哈希。仅复用未结束任务，
    不缓存已完成结果，也不替代用户鉴权；所有身份和链参数必须包含在调用参数中。
    """

    @wraps(fn)
    async def wrapped(*args, **kwargs):
        """查找当前循环内相同参数的在途任务，没有则创建；等待者共享结果或异常。"""
        key = (asyncio.get_running_loop(), fn, args, tuple(sorted(kwargs.items())))
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(fn(*args, **kwargs))
            _inflight[key] = task

            def done(completed):
                """任务结束后删除在途登记并读取异常，防止等待者全部断开后出现无人处理的异常。"""
                _inflight.pop(key, None)
                # 即使所有等待者已经断开，也主动读取异常，避免后台错误无人处理。
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(done)
        # 单个请求取消只结束自己的等待，不取消其他请求仍需要的共享读取任务。
        return await asyncio.shield(task)

    return wrapped
