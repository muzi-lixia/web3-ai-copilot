"""链上读取：RPC 多节点失败转移 + Multicall3 批量读取。

单独一层的两个理由：

1. **公共 RPC 会挂。** URL 写死会让"某个节点抽风"直接升级成"功能不可用"。
   这里按序试下一个，并记住最后一个可用的，避免每次请求都重撞同一堵墙。

2. **逐个 balanceOf 是 N 次 RPC 往返。** Multicall3 把 N 次调用合成 1 次
   `eth_call`，而且所有结果取自**同一个区块** —— 余额之间不会出现跨区块错位。

这一层只认 `ChainMeta`，不认具体是哪条链：换链、加链都不需要改这里。
"""

from collections.abc import Callable
from typing import TypeVar

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout
from web3 import Web3
from web3.exceptions import ContractLogicError

from app.core.config import settings
from app.core.exceptions import InvalidAddressError, UpstreamError
from app.core.logging import get_logger
from app.infrastructure.blockchain.chains import ChainMeta
from app.modules.asset.tokens import TokenMeta

logger = get_logger(__name__)

RPC_TIMEOUT_SECONDS = 12


def to_checksum(owner: str) -> str:
    """归一化为 EIP-55 地址。

    放在这一层而不是各服务里各写一遍：地址进出的格式只有一处口径，
    才不会出现"同一个地址在资产接口通过了、在质押接口被拒"这种怪事。

    格式非法一律转成领域错误（400），不外泄 web3 的异常类型 ——
    web3 对非法地址抛的异常类型不止一种，漏掉一种就会变成 500。
    """
    try:
        return Web3.to_checksum_address(owner)
    except Exception as exc:  # noqa: BLE001
        raise InvalidAddressError(f"地址格式不合法：{owner}") from exc


MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]

BALANCE_OF_SELECTOR = Web3.keccak(text="balanceOf(address)")[:4]
"""`balanceOf(address)` 的 4 字节选择器。

手工构造 calldata 而非用 `Contract.encode_abi`：web3 v8 已移除该方法，
直接拼字节不受这个层面的版本变动影响。
"""

# 每条链记住最后一个连通的节点。换节点等于换数据源，不同节点间存在区块高度差，
# 频繁切换会让同一个钱包的余额忽高忽低。
_good_rpc: dict[int, str] = {}


def _candidate_urls(chain: ChainMeta) -> list[str]:
    """候选节点顺序：env 覆盖优先，其次链注册表里的公共节点；上次成功的排最前。"""
    overridden = settings.rpc_override_map.get(chain.chain_id)
    urls = overridden if overridden else list(chain.rpc_urls)
    preferred = _good_rpc.get(chain.chain_id)
    if preferred and preferred in urls:
        return [preferred, *(u for u in urls if u != preferred)]
    return urls


def connect(chain: ChainMeta) -> Web3:
    """返回一个已确认可用的 Web3；全部节点不可用则抛 UpstreamError。

    每次连接都真发一次请求（读 chain_id），而不是只看 `is_connected()`：
    **能连上不等于连对了链。** 如果节点的 chain_id 与目标链不符就换下一个 ——
    否则会把另一条链上的余额当成本链余额返回，数据看起来完全正常，
    却是这套逻辑里最严重的一种错。
    """
    failures: list[str] = []

    for index, url in enumerate(_candidate_urls(chain), start=1):
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))
            actual_chain_id = w3.eth.chain_id
        except Exception as exc:  # noqa: BLE001 —— 节点故障种类很多，逐个记录后换下一个
            failures.append(f"节点{index} ({type(exc).__name__})")
            continue

        if actual_chain_id != chain.chain_id:
            failures.append(f"节点{index} (chain_id={actual_chain_id}，期望 {chain.chain_id})")
            continue

        _good_rpc[chain.chain_id] = url
        logger.debug("%s 使用节点 %s", chain.name, index)
        return w3

    raise UpstreamError(f"{chain.name} 的 RPC 节点全部不可用：{'; '.join(failures)}")


def read_native_balance(w3: Web3, owner: str, block_identifier="latest") -> int:
    """原生币余额，单位是最小面额（wei）。"""
    return int(w3.eth.get_balance(owner, block_identifier=block_identifier))


def read_erc20_balances(
    w3: Web3,
    chain: ChainMeta,
    tokens: tuple[TokenMeta, ...],
    owner: str,
    block_identifier="latest",
) -> dict[str, int]:
    """批量读 ERC-20 余额。返回 `{合约地址小写: 原始整数余额}`。

    传全部候选（含原生币），函数内部自己挑出可走合约调用的那些。

    `allowFailure=True`：单个 token 读失败（合约异常、非标准实现）只丢它自己。
    整批失败是不可接受的 —— 一个坏地址会让钱包显示成"一个币都没有"，
    而用户无法从界面上看出这是读失败而不是真的没钱。
    失败项不进返回值，调用方必须保留未知状态。
    """
    # 一并取出 checksum 地址：后面拼 calldata 和写结果 map 都要用，
    # 在这里判一次空，后面两步就不必再判。
    targets = [(meta, Web3.to_checksum_address(meta.address)) for meta in tokens if meta.address]
    if not targets:
        return {}

    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(chain.multicall3),
        abi=MULTICALL3_ABI,
    )
    # owner 参数编一次即可，不必每个 token 重编
    encoded_owner = w3.codec.encode(["address"], [owner])
    calls = [(address, True, BALANCE_OF_SELECTOR + encoded_owner) for _, address in targets]

    results = multicall.functions.aggregate3(calls).call(block_identifier=block_identifier)

    balances: dict[str, int] = {}
    for (meta, address), (success, return_data) in zip(targets, results, strict=True):
        if not success or len(return_data) < 32:
            logger.warning("链上读取 %s(%s) 余额失败，标记为未知", meta.symbol, address)
            continue
        balances[address.lower()] = int(w3.codec.decode(["uint256"], return_data)[0])
    return balances


_T = TypeVar("_T")


def read_with_failover(chain: ChainMeta, operation: Callable[[Web3], _T]) -> _T:
    """在传输故障时换节点重新执行整段只读操作。

    每个候选节点先核对 chain_id，成功后记住节点。合约逻辑错误、业务错误和
    非传输异常不会换节点重试，因为更换节点不能修复确定性的合约或参数问题。
    operation 必须幂等且只读，不能把转账等有副作用操作放进此重试入口。
    """
    for index, url in enumerate(_candidate_urls(chain), start=1):
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))
            if w3.eth.chain_id != chain.chain_id:
                continue
            result = operation(w3)
            _good_rpc[chain.chain_id] = url
            return result
        except (Timeout, RequestsConnectionError, ConnectionError, TimeoutError) as exc:
            logger.warning("%s 节点 %s 传输失败 (%s)", chain.name, index, type(exc).__name__)
        except ContractLogicError:
            raise UpstreamError(f"{chain.name} 合约读取失败") from None
        except UpstreamError:
            raise
        except Exception as exc:
            logger.warning("%s 读取失败 (%s)", chain.name, type(exc).__name__)
            raise UpstreamError(f"{chain.name} 链上数据读取失败") from None
    raise UpstreamError(f"{chain.name} RPC 节点全部不可用")
