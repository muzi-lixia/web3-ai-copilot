"""Exact base-unit conversion and explicit precision for monetary arithmetic."""

from decimal import Decimal, localcontext
from functools import wraps
from inspect import iscoroutinefunction


def to_human(raw: int, decimals: int) -> Decimal:
    if decimals < 0 or decimals > 255:
        raise ValueError("decimals must be in 0..255")
    if raw == 0:
        return Decimal(0)
    value = Decimal(raw).as_tuple()
    return Decimal((value.sign, value.digits, -decimals))


def monetary_calculation(fn):
    """512 significant digits cover uint256 amounts, scaling and monetary products.

    Local contexts do not change the caller's Decimal context. Display rounding
    remains explicit at the individual output boundary.
    """
    if iscoroutinefunction(fn):

        @wraps(fn)
        async def asynchronous(*args, **kwargs):
            with localcontext() as ctx:
                ctx.prec = 512
                return await fn(*args, **kwargs)

        return asynchronous

    @wraps(fn)
    def synchronous(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 512
            return fn(*args, **kwargs)

    return synchronous
