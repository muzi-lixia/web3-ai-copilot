"""数值归一化：链上最小单位整数 ↔ 人类可读的 `Decimal`。

单独成一个模块，是因为它被资产、质押两处使用 —— 归一化口径必须只有一份。
抄第二份的话，两处对"零怎么表示"的处理迟早会分叉，而那种偏差表现为
「同一个 0，一边显示 `0`、一边显示 `0.000000000000000000`」。

不碰网络、不碰 RPC，可以直接单测。
"""

from decimal import Decimal


def to_human(raw: int, decimals: int) -> Decimal:
    """最小面额整数 → 人类可读数量。

    用 `scaleb(-decimals)` 而不是 `raw / 10**decimals`：除以 10 的幂在十进制下
    也是精确的，但先把结果落成 Decimal 能确保**整条链路不经过 float**
    —— 从 bigint 到展示，这是唯一可能引入精度损失的地方。

    零单独处理：`Decimal(0).scaleb(-18)` 会保留指数（`0E-18`），
    序列化出来是 `0.000000000000000000`。前端要用 `amount === "0"` 判断
    "这个币没有余额"，所以零必须归一成干净的 `0`。
    """
    if raw == 0:
        return Decimal(0)
    return Decimal(raw).scaleb(-decimals)
