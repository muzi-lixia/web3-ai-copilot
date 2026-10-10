"""链注册表。

**新增一条链 = 加一个 ChainMeta 条目**，读取层（infra/blockchain/chain.py）、
资产服务、路由都不用改。这是"链可配置"的落点。

各链上有哪些 token 不在这里，见 `modules/asset/tokens.py` —— 两者分开是因为
变化频率不同：换 RPC 偶尔发生，加币很频繁；混在一处会让每次加币都要翻整份链参数。
"""

from dataclasses import dataclass

from app.core.exceptions import UnsupportedChainError


@dataclass(frozen=True, slots=True)
class ChainMeta:
    """一条链的静态能力配置。

    RPC、浏览器和 Multicall 属于链元数据；行情源链标识分别使用各提供方
    命名，不能把数字 chain_id 直接当成第三方 URL 参数。私有 RPC 凭据走环境覆盖。
    """

    key: str
    """短名，同时用作前端 / 日志里的链标识。"""

    chain_id: int

    name: str
    """展示名。"""

    multicall3: str
    """Multicall3 部署地址。它是 CREATE2 确定性地址，绝大多数 EVM 链相同，
    但仍逐链显式登记 —— 依赖"应该一样"是地址类错误的典型来源。"""

    rpc_urls: tuple[str, ...]
    """按序失败转移。只放公开节点；带 key 的私有节点走 settings.rpc_overrides，
    不要写进这个文件（它是要提交到仓库的）。"""

    explorer: str
    """区块浏览器根地址，随响应下发给前端拼地址链接。"""

    dexscreener_id: str
    """DexScreener 的链标识（行情查询用，不是 chain_id 的字符串形式）。

    **必须带它查**：实测同一个 USDT0 合约地址在 Mantle / Stable / Berachain
    上都有部署，按地址裸查会一次返回三条链的池子，且 Mantle 上的流动性比
    Berachain 高一个量级 —— 不按链过滤，Berachain 的钱包会读到别的链的价格。
    """

    defillama_id: str
    """DefiLlama 的链标识，用于 `coins.llama.fi/prices/current/{id}:{addr}`。

    两者分开登记，是因为它们是两个互相独立的外部系统，各自的命名规则不归我们管。
    新增链时两个标识都要去各自的文档/接口上核对，不能因为某条链碰巧同名就默认通用。
    """


# 原生币（symbol / decimals）**不在这里**，而在 tokens.py 里以 address=None
# 的 TokenMeta 形式登记 —— 它和 ERC-20 一样要参与余额归一化，放两处必然出现
# "改了 A 忘了改 B"的静默不一致。


BERACHAIN = ChainMeta(
    key="berachain",
    chain_id=80094,
    name="Berachain",
    multicall3="0xcA11bde05977b3631167028862bE2a173976CA11",
    rpc_urls=(
        "https://rpc.berachain.com",
        "https://berachain-rpc.publicnode.com",
        "https://berachain.drpc.org",
        "https://rpc.berachain-apis.com",
    ),
    explorer="https://berascan.com",
    dexscreener_id="berachain",
    defillama_id="berachain",
)


def get_chain(chain_id: int) -> ChainMeta:
    """按 chain_id 取链配置。未登记抛 UnsupportedChainError。

    不返回 None 让调用方自己判空：链是必填上下文，缺了就该立刻失败，
    而不是让后续逻辑带着 None 走进"读到空数据"的分支。
    """
    chain = BY_ID.get(chain_id)
    if chain is None:
        supported = ", ".join(f"{c.key}({c.chain_id})" for c in CHAINS)
        raise UnsupportedChainError(f"暂不支持的链 {chain_id}，当前可用：{supported}")
    return chain


# 保留既有 Berachain 业务配置，追加需求中的五条 EVM 网络。生产节点走 RPC_OVERRIDES。
# 公共节点仅作开发默认；所有 RPC 调用会校验实际 chain_id，不能靠 URL 名称判断网络。
def _evm(key, cid, name, urls, explorer, dex, llama):
    return ChainMeta(key, cid, name, BERACHAIN.multicall3, urls, explorer, dex, llama)


ETHEREUM = _evm(
    "ethereum",
    1,
    "Ethereum",
    ("https://ethereum-rpc.publicnode.com",),
    "https://etherscan.io",
    "ethereum",
    "ethereum",
)
BSC = _evm("bsc", 56, "BSC", ("https://bsc-dataseed.bnbchain.org",), "https://bscscan.com", "bsc", "bsc")
POLYGON = _evm(
    "polygon",
    137,
    "Polygon",
    ("https://polygon-bor-rpc.publicnode.com",),
    "https://polygonscan.com",
    "polygon",
    "polygon",
)
ARBITRUM = _evm(
    "arbitrum",
    42161,
    "Arbitrum",
    ("https://arb1.arbitrum.io/rpc",),
    "https://arbiscan.io",
    "arbitrum",
    "arbitrum",
)
BASE = _evm("base", 8453, "Base", ("https://mainnet.base.org",), "https://basescan.org", "base", "base")
CHAINS = (BERACHAIN, ETHEREUM, BSC, POLYGON, ARBITRUM, BASE)
BY_ID = {c.chain_id: c for c in CHAINS}
