"""质押金库的链上读取（EVM 侧）。

只做一件事：把「一个 `StakingModule` + 一个地址」变成一组原始整数。
不碰 USD、不碰 APY（APY 不在链上）、不碰第三方接口 —— 那些在 `staking_service`。

## 为什么手拼 calldata 而不用 ABI

与 `client.py` 读 ERC-20 余额同样的理由，另加一条这里特有的：

`getERC721WithdrawalRequestIds` 在合约里是**重载**的 —— `(address)` 与
`(address,uint256,uint256)` 并存。走 `contract.functions.xxx` 在重载时会直接抛错，
而靠 ABI 列表顺序去猜更是不该做的事。选择器从**完整签名文本**算出来就没有歧义。

## 两轮 Multicall

第二轮依赖第一轮（折算要知道份额是多少，取请求详情要知道请求 ID 有哪些），
压不成一轮。两轮各自都是单次 `eth_call`，并显式使用同一个固定区块 ——
份额与折算、请求 ID 与请求详情之间不会出现跨区块错位。
"""

from dataclasses import dataclass

from web3 import Web3

from app.core.exceptions import UpstreamError
from app.core.logging import get_logger
from app.infrastructure.blockchain.chains import ChainMeta
from app.infrastructure.blockchain.client import MULTICALL3_ABI
from app.modules.staking.constants import StakingModule

logger = get_logger(__name__)


def _selector(signature: str) -> bytes:
    """从**完整签名**（带参数类型）算 4 字节选择器。只靠函数名无法区分重载。"""
    return Web3.keccak(text=signature)[:4]


_SEL_BALANCE_OF = _selector("balanceOf(address)")
_SEL_CONVERT_TO_ASSETS = _selector("convertToAssets(uint256)")
_SEL_TOTAL_ASSETS = _selector("totalAssets()")
_SEL_TOTAL_SUPPLY = _selector("totalSupply()")
_SEL_ASSET = _selector("asset()")
_SEL_COOLDOWN = _selector("WITHDRAWAL_COOLDOWN()")
_SEL_REQUEST_IDS = _selector("getERC721WithdrawalRequestIds(address)")
_SEL_GET_REQUEST = _selector("getERC721WithdrawalRequest(uint256)")

_REQUEST_WORDS = 5
"""struct WithdrawalRequest 占 5 个字（3×uint256 + 2×address，全是定长）。

字段顺序抄自 `contracts/src/pol/interfaces/IWBERAStakerVaultWithdrawalRequest.sol`：

    uint256 assets; uint256 shares; uint256 requestTime; address owner; address receiver;

**顺序错了不会报错** —— 解码照样成功，只是把 owner 当成 receiver、
把区块时间当成金额，界面上每个数字看起来都合理。合约升级后先核对这里。
"""


@dataclass(frozen=True, slots=True)
class PendingWithdrawal:
    """一笔排队中的赎回。金额是**锁定值**，不是当前市值。"""

    request_id: int
    assets: int
    """发起时就锁定的底层资产数量（最小单位）。解绑期内不计收益，所以它不随时间增长
    —— 这正是"锁"字的含义。"""

    shares: int
    """发起时烧掉的份额。份额在排队时**就已经烧了**，所以它不再出现在 balanceOf 里。"""

    requested_at: int
    """unix 秒。"""

    owner: str
    receiver: str

    def unlock_at(self, cooldown_seconds: int) -> int:
        """按请求发起的 Unix 秒加解绑时长，得到可提取时刻；不读取本地当前时间。"""
        return self.requested_at + cooldown_seconds


@dataclass(frozen=True, slots=True)
class VaultReading:
    """一次金库读取的全部原始结果。单位一律是最小单位整数。"""

    shares: int
    """用户持有的金库份额（sWBERA）。"""

    underlying: int
    """`convertToAssets(shares)` 的结果。

    **这一步不能省。** sWBERA 与 WBERA 不是 1:1（实测 1 sWBERA ≈ 1.467 WBERA），
    直接拿份额当底层数量会低估约 32%，而且不会报任何错。
    """

    asset: str
    """金库底层资产的合约地址，即链上 `asset()` 的真实返回值。读取失败拒绝构建仓位，不猜测底层资产。"""

    total_assets: int
    total_supply: int
    """用于独立复核汇率：两者相除应等于 underlying / shares。"""

    cooldown_seconds: int | None
    """链上的解绑时长。读不到时为 None —— 调用方回退到注册表里的声明值，
    而不是当成 0（那会让所有排队中的请求立刻显示"已可提取"）。"""

    pending: tuple[PendingWithdrawal, ...]


def read_vault(w3: Web3, chain: ChainMeta, module: StakingModule, owner: str) -> VaultReading:
    """读取 owner 在该金库上的份额、折算数量与全部排队中的赎回。"""
    vault = Web3.to_checksum_address(module.vault)
    holder = Web3.to_checksum_address(owner)
    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(chain.multicall3),
        abi=MULTICALL3_ABI,
    )

    block = w3.eth.block_number
    encoded_owner = w3.codec.encode(["address"], [holder])

    # ── 第一轮：份额、金库全局统计、解绑时长、该地址的请求 ID ──
    first = multicall.functions.aggregate3(
        [
            (vault, True, _SEL_BALANCE_OF + encoded_owner),
            (vault, True, _SEL_TOTAL_ASSETS),
            (vault, True, _SEL_TOTAL_SUPPLY),
            (vault, True, _SEL_ASSET),
            (vault, True, _SEL_COOLDOWN),
            (vault, True, _SEL_REQUEST_IDS + encoded_owner),
        ]
    ).call(block_identifier=block)

    shares = _word(first[0], "balanceOf(sWBERA)")
    total_assets = _word(first[1], "totalAssets")
    total_supply = _word(first[2], "totalSupply")
    asset = _address(first[3], "asset()")
    request_ids = _uint_list(first[5], "getERC721WithdrawalRequestIds")
    if not _ok(first[4]):
        cooldown = None
        logger.warning("链上读取 WITHDRAWAL_COOLDOWN 失败，本次用注册表声明的时长")
    else:
        cooldown = _word(first[4], "WITHDRAWAL_COOLDOWN")

    # ── 第二轮：份额折算 + 每笔请求的详情 ──
    calls = [
        (vault, True, _SEL_CONVERT_TO_ASSETS + w3.codec.encode(["uint256"], [shares])),
        *(
            (vault, True, _SEL_GET_REQUEST + w3.codec.encode(["uint256"], [request_id]))
            for request_id in request_ids
        ),
    ]
    second = multicall.functions.aggregate3(calls).call(block_identifier=block)

    underlying = _word(second[0], "convertToAssets")

    pending = [
        request
        for request_id, result in zip(request_ids, second[1:], strict=True)
        if (request := _request(result, request_id)) is not None
    ]

    return VaultReading(
        shares=shares,
        underlying=underlying,
        asset=asset,
        total_assets=total_assets,
        total_supply=total_supply,
        cooldown_seconds=cooldown,
        pending=tuple(pending),
    )


def _ok(result: object) -> bool:
    """Multicall 返回二元组且 success 为真正布尔 True，才认定调用成功。"""
    return isinstance(result, (list, tuple)) and len(result) == 2 and result[0] is True


def _raw(result: object) -> bytes:
    """只接收 ABI 字节数据；非法类型不通过 bytes(int) 等隐式构造伪造数据。"""
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        return b""
    return bytes(result[1]) if isinstance(result[1], (bytes, bytearray)) else b""


def _word(result: object, label: str) -> int:
    """解码主体 uint256；失败或长度错误抛异常，合法编码的零才表示真实零余额。"""
    data = _raw(result)
    if not _ok(result) or len(data) != 32:
        raise UpstreamError(f"{label} 读取失败，仓位数据不完整")
    return int.from_bytes(data, "big")


def _address(result: object, label: str) -> str:
    """解码底层资产地址；缺失、非标准编码及零地址不能用于估值。"""
    data = _raw(result)
    if not _ok(result) or len(data) != 32 or any(data[:12]) or not any(data[12:]):
        raise UpstreamError(f"{label} 读取失败，底层资产不可确认")
    return Web3.to_checksum_address(data[12:])


def _uint_list(result: object, label: str) -> list[int]:
    """严格解码单个 uint256[]，校验偏移、长度与完整数据；失败不代表没有提款。"""
    data = _raw(result)
    if not _ok(result) or len(data) < 64 or len(data) % 32:
        raise UpstreamError(f"{label} 读取失败，提款队列不完整")
    # 单个动态返回值的标准偏移量为一个 ABI 字，必须能定位数组长度。
    offset = int.from_bytes(data[:32], "big")
    if offset != 32:
        raise UpstreamError(f"{label} 返回偏移异常，提款队列不完整")
    length = int.from_bytes(data[offset : offset + 32], "big")
    start = offset + 32
    if start + length * 32 != len(data):
        raise UpstreamError(f"{label} 返回长度异常，提款队列不完整")
    return [int.from_bytes(data[start + i * 32 : start + (i + 1) * 32], "big") for i in range(length)]


def _request(result: object, request_id: int) -> PendingWithdrawal | None:
    """解码一笔提款请求；不存在的 ID 返回 None。"""
    data = _raw(result)
    if not _ok(result) or len(data) < _REQUEST_WORDS * 32:
        raise UpstreamError(f"提款请求 {request_id} 读取失败，仓位估值不完整")

    raw_owner = data[3 * 32 + 12 : 4 * 32]
    raw_receiver = data[4 * 32 + 12 : 5 * 32]

    # 不存在的请求 → 合约返回全零 struct（接口注释里写明了），而不是 revert。
    # 零地址 owner 是它最可靠的标志：真实请求的 owner 不可能是零地址，
    # 那意味着没人拥有它。先比原始字节再转 checksum —— 转完比字符串容易写错大小写。
    if not any(raw_owner):
        return None

    return PendingWithdrawal(
        request_id=request_id,
        assets=int.from_bytes(data[0:32], "big"),
        shares=int.from_bytes(data[32:64], "big"),
        requested_at=int.from_bytes(data[64:96], "big"),
        owner=Web3.to_checksum_address(raw_owner),
        receiver=Web3.to_checksum_address(raw_receiver),
    )
