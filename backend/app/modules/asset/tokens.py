"""各链的候选 token 清单。

存在的理由：**链上不存在「某地址持有哪些 token」这个调用。**
EVM 只能回答「地址 X 在合约 Y 上的余额是多少」，无法枚举持有关系。
所以必须先有一份候选清单，再逐个 balanceOf（由 Multicall 合成一次调用）。

这份表承担两个职责：
  1. 决定**读哪些合约**（`address`）
  2. 提供**归一化参数**（`decimals`）—— 存下来直接用，省掉每次都去链上问一遍

⚠️ 地址与 decimals 是手写的，写错**不会报错**：只会静默读到空，或者读到
   一个完全不相干的合约（给出看起来合理但实际错误的数据）。
   新增条目必须跑 `scripts/verify_constants.py` 与链上 `symbol()` / `decimals()`
   核对一致后再提交。

新增链时：先在 `chains.py` 登记链参数，再在这里加一份该链的清单，
并挂进 `TOKENS_BY_CHAIN`。两处都加，是因为两者变化频率不同。
"""

from dataclasses import dataclass
from typing import Literal

from app.core.exceptions import NotFoundError, UnsupportedChainError
from app.infrastructure.blockchain.chains import BERACHAIN

RiskClass = Literal["stable", "volatile"]
"""风险分析用的资产分类。

**只有两类且互斥、穷尽**，这是刻意的：分类越细，越容易出现"各档之和不到 100%"
的窟窿，而那个窟窿既解释不清也没法修（总有一批币落不进任何一档）。
稳定币之外一律算高波动，于是两个比率天然互补。

默认 `volatile`：漏标的币被算成高波动，方向上偏保守 ——
把风险资产误判成低风险，代价远大于反过来。
"""


@dataclass(frozen=True, slots=True)
class TokenMeta:
    """候选资产注册项，描述展示名、链上地址、精度及风险分类。

    address=None 表示原生币；行情可借包装币地址查询。该清单是显式支持
    范围，不是扫描钱包所有未知代币；增加资产时应先核对链上地址与 decimals。
    """

    symbol: str

    address: str | None
    """ERC-20 合约地址。

    `None` 表示该链的**原生币**（走 `eth_getBalance`，不能进 Multicall，
    因为它根本没有合约）。资产条目里 `contract` 字段为 null 且 `kind="native"`
    就是由它决定的。
    """

    decimals: int
    """归一化位数。原生币没有合约可问，只能写在表里。"""

    price_address: str | None = None
    """查行情时用哪个链上地址（见 `price_address_of`）。

    **只有原生币需要显式填。** 它在链上没有合约，行情接口无从查起，只能借用
    等价的包装币作代理 —— 包装币与原生币 1:1 锚定、赎回无摩擦，价格不会脱钩。
    其余 token 留空即可，默认用自己的合约地址。
    """

    risk_class: RiskClass = "volatile"
    """风险分类（见 `RiskClass`）。只有稳定币需要显式标注，其余留默认。"""

    onchain_symbol: str | None = None
    """合约 `symbol()` 实际返回的字符串，**只在与 `symbol` 不同时填**。

    `symbol` 是我们要展示、也是接口里当键用的名字；`onchain_symbol` 是合约自称的名字。
    **两者本来就允许不同** —— 已实测两例（2026-10-07，见各自条目）：

        USDT0  合约写的是 `USD₮0`（U+20AE 蒙古图格里克符号，不是字母 T）
        HONEY  合约写的是 `BUSD`（name 为 "Bera USD"，社区后来都叫它 HONEY）

    留这个字段是为了**让核对脚本保持严格**：任何没登记过的名称差异依然报错。
    若改成"名称不一致一律放过"，那么"地址填成了另一个合约"就再也测不出来了 ——
    那种错误在症状上正好就是名称对不上。
    """


# ── Berachain（chain_id 80094）──────────────────────────────────────────────
# 顺序即展示顺序。原生币放第一位：它是 gas，也是识别这条链的第一眼标识。
#
# ✅ 核对于 2026-10-07（`scripts/verify_constants.py`）：8 个合约地址全部有代码、
#    decimals 与链上 `decimals()` 逐条一致，行情源 8 个取到价（BVT 无池是事实，不是故障），
#    质押模块四项全对。仅两处名称与链上写法不同，属**已登记的别名**（见 `onchain_symbol`）。
_WBERA = "0x6969696969696969696969696969696969696969"

BERACHAIN_TOKENS: tuple[TokenMeta, ...] = (
    # 行情借用 WBERA：BERA 是原生币，没有合约地址可查。
    # 注：BVT 没有任何交易池，所有免费源都给不出价 → 它单独出现在响应的 missing 字段里，
    # 这是事实而不是读取失败。BGT 也不可转让、DexScreener 上没有池子，但 **DefiLlama 有它的价**
    # （实测 0.2246）—— 所以 missing 里只会有 BVT 一个，别把这两者混为一谈。
    TokenMeta("BERA", None, 18, price_address=_WBERA),
    TokenMeta("WETH", "0x2F6F07CDcf3588944Bf4C42aC74ff24bF56e7590", 18),
    TokenMeta("BVT", "0x7dC8013Bc53d3bf0298127CD828a20815581AAba", 18),
    # Berachain 上的 USDT 由 USDT0 承载（无被拆分的桥接版）。
    # 链上 symbol 是 `USD₮0` —— 用 U+20AE（蒙古图格里克符号）冒充字母 T，是 Tether 官方写法。
    # 表里写 ASCII 的 `USDT0`：这个串要拼进查询参数当键用，掺非 ASCII 只会自找麻烦。
    TokenMeta(
        "USDT0",
        "0x779Ded0c9e1022225f8E0630b35a9b54bE713736",
        6,
        risk_class="stable",
        onchain_symbol="USD₮0",
    ),
    # 桥接版 USDC，链上常记作 USDC.e。
    TokenMeta("USDC.e", "0x549943e04f40284185054145c6E4e9568C1D3241", 6, risk_class="stable"),
    # Berachain 原生稳定币，超额抵押锚定美元 —— 它是本链上的主要结算资产，
    # 与桥接进来的 USDC.e / USDT0 并列计入稳定币缓冲。
    # 合约自称 `BUSD` / "Bera USD"（DexScreener 也跟着叫 BUSD，连 CoinGecko 挂的图都还是 BUSD 的），
    # 但社区与 Berachain 自己一律叫 HONEY（GeckoTerminal 的 symbol 就是 HONEY）。
    # 展示名取 HONEY —— 用户认的是这个名字。
    TokenMeta(
        "HONEY",
        "0xFCBD14DC51f0A4d49d5E53C2E0950e0bC26d0Dce",
        18,
        risk_class="stable",
        onchain_symbol="BUSD",
    ),
    TokenMeta("WBERA", _WBERA, 18),
    TokenMeta("WBTC", "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c", 8),
    TokenMeta("BGT", "0x656b95E550C07a9ffe548bd4085c72418Ceb1dba", 18),
)


TOKENS_BY_CHAIN: dict[int, tuple[TokenMeta, ...]] = {
    BERACHAIN.chain_id: BERACHAIN_TOKENS,
}


def tokens_for(chain_id: int) -> tuple[TokenMeta, ...]:
    """取该链的候选清单。

    注意：链参数在 `chains.py` 登记、清单在这里登记，两处可能不同步。
    这里显式报错而不是让它变成 KeyError，是为了让"加了链却忘了加币"
    这种疏漏一眼可见。
    """
    tokens = TOKENS_BY_CHAIN.get(chain_id)
    if tokens is None:
        raise UnsupportedChainError(f"链 {chain_id} 尚未登记 token 清单（见 constants/tokens.py）")
    return tokens


def erc20_tokens(tokens: tuple[TokenMeta, ...]) -> tuple[TokenMeta, ...]:
    """可进 Multicall 批量读取的部分（排除原生币）。"""
    return tuple(t for t in tokens if t.address is not None)


def native_token(tokens: tuple[TokenMeta, ...]) -> TokenMeta | None:
    """该链的原生币条目。清单里最多一条，没有则返回 None。"""
    return next((t for t in tokens if t.address is None), None)


def price_address_of(token: TokenMeta) -> str | None:
    """查行情时该用哪个链上地址。

    优先显式指定的 `price_address`（原生币），否则用它自己的合约地址。
    两者都没有说明这个 token 没有可查行情的地址，返回 None ——
    调用方应把它归入"取不到行情"，而不是当成价格为 0。
    """
    return token.price_address or token.address


def risk_class_of(chain_id: int, symbol: str) -> RiskClass:
    """查某条链上某个 symbol 的风险分类。

    清单外的 symbol 按 `volatile` 兜底：它理论上不会出现（资产条目本来就出自
    同一份清单），真出现了说明两处不同步 —— 此时按高波动算，方向上偏保守。
    """
    for token in tokens_for(chain_id):
        if token.symbol == symbol:
            return token.risk_class
    return "volatile"


def select_tokens(chain_id: int, symbols: tuple[str, ...] | None = None) -> tuple[TokenMeta, ...]:
    """精确按符号选择读取对象，BERA 与 WBERA 不能用包含关系匹配。

    None 表示候选清单；明确指定但不支持的币种返回错误，不退回全部资产或零余额。
    普通 API 与 Agent 可复用该选择能力，不引入任何模型/工具框架依赖。
    """
    tokens = tokens_for(chain_id)
    if symbols is None:
        return tokens
    wanted = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
    by_symbol = {token.symbol.upper(): token for token in tokens}
    missing = [symbol for symbol in wanted if symbol not in by_symbol]
    if not wanted or missing:
        raise NotFoundError("当前网络未支持查询币种：" + ", ".join(missing or ["空币种列表"]))
    return tuple(by_symbol[symbol] for symbol in wanted)


# 新网络首期覆盖原生币与主流稳定币，范围显式登记，不宣称自动发现全部资产。
# 原生币估价使用包装币报价；支持链不等于已核验所有公共 RPC 的生产 SLA。
TOKENS_BY_CHAIN.update(
    {
        1: (
            TokenMeta("ETH", None, 18, price_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
            TokenMeta("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, risk_class="stable"),
            TokenMeta("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, risk_class="stable"),
        ),
        56: (
            TokenMeta("BNB", None, 18, price_address="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
            TokenMeta("USDT", "0x55d398326f99059fF775485246999027B3197955", 18, risk_class="stable"),
        ),
        137: (
            TokenMeta("POL", None, 18, price_address="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"),
            TokenMeta("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6, risk_class="stable"),
        ),
        42161: (
            TokenMeta("ETH", None, 18, price_address="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
            TokenMeta("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6, risk_class="stable"),
        ),
        8453: (
            TokenMeta("ETH", None, 18, price_address="0x4200000000000000000000000000000000000006"),
            TokenMeta("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6, risk_class="stable"),
        ),
    }
)
