"""Merge concurrent identical reads; do not cache results or bind locks across loops."""

import asyncio
from functools import wraps

_inflight: dict[tuple, asyncio.Task] = {}


def singleflight(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        key = (asyncio.get_running_loop(), fn, args, tuple(sorted(kwargs.items())))
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(fn(*args, **kwargs))
            _inflight[key] = task

            def done(completed):
                _inflight.pop(key, None)
                # A disconnected caller must not leave unobserved task exceptions.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(done)
        # One caller disconnecting must not cancel the other callers' shared read.
        return await asyncio.shield(task)

    return wrapped
