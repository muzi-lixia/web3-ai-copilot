"""链上最小单位的精确转换与金额计算上下文。

数量和金额保持 Decimal，只有展示比率才转换 float；这里不决定前端显示的小数位数。
"""

from decimal import Decimal, localcontext
from functools import wraps
from inspect import iscoroutinefunction


def to_human(raw: int, decimals: int) -> Decimal:
    """将链上最小单位整数转换为精确 Decimal 数量。

    decimals 是代币精度，例如 18 表示整数乘以 10 的负 18 次方。直接构造
    Decimal 的数字与指数，避免除法受默认精度限制而提前舍入；不经过 float。
    """
    if decimals < 0 or decimals > 255:
        raise ValueError("decimals must be in 0..255")
    if raw == 0:
        return Decimal(0)
    value = Decimal(raw).as_tuple()
    return Decimal((value.sign, value.digits, -decimals))


def monetary_calculation(fn):
    """给同步或异步金额计算提供 512 位有效数字的局部精度。

    链上 uint256 最多约 78 位十进制数字，缩放、乘法和汇总需要比默认 28 位更大
    的余量。localcontext 隔离调用方上下文，退出时自动恢复；它不替代输出边界
    明确的舍入规则，也不自动格式化金额。
    """
    if iscoroutinefunction(fn):

        @wraps(fn)
        async def asynchronous(*args, **kwargs):
            """在独立高精度 Decimal 上下文内等待异步计算，退出时恢复调用方上下文。"""
            with localcontext() as ctx:
                ctx.prec = 512
                return await fn(*args, **kwargs)

        return asynchronous

    @wraps(fn)
    def synchronous(*args, **kwargs):
        """在独立高精度 Decimal 上下文内执行同步计算，不改变全局小数精度。"""
        with localcontext() as ctx:
            ctx.prec = 512
            return fn(*args, **kwargs)

    return synchronous
