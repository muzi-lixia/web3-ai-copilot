"""钱包资产查询：把链上余额组装成前端契约。

分两步，两步的**失败语义完全不同**，所以分开写也分开处理：

    1. 读余额（`_read_holdings`）—— 走自家 RPC，失败就是失败，向上抛
    2. 读行情（`_load_quotes`）  —— 走第三方行情源，失败降级为空表，不抛

第二步失败不能让第一步的结果消失。"资产"这件事的主体是余额，
估值只是加在它上面的一层注解 —— 注解没盖章，正文照样要交。
"""

import asyncio
from datetime import UTC, datetime

from anyio.to_thread import run_sync as run_in_threadpool
from web3 import Web3

from app.common.schemas import DataIssue
from app.common.singleflight import singleflight
from app.common.units import to_human
from app.core.exceptions import UpstreamError
from app.core.logging import get_logger
from app.core.resources import BusinessResources
from app.infrastructure.blockchain import client as chain_service
from app.infrastructure.blockchain.chains import ChainMeta, get_chain
from app.infrastructure.blockchain.client import to_checksum
from app.modules.asset.schemas import Asset, WalletAssets
from app.modules.asset.tokens import erc20_tokens, native_token, select_tokens
from app.modules.asset.valuation import value_assets
from app.modules.market import service as market_service
from app.modules.market.schemas import TokenMarket

logger = get_logger(__name__)


def _read_holdings(
    chain: ChainMeta,
    address: str,
    symbols: tuple[str, ...] | None = None,
    *,
    resources: BusinessResources | None = None,
) -> list[Asset]:
    """链上读取（同步阻塞）。返回选定币种；未指定才返回全部候选，保留零余额。

    不在这里过滤零余额 —— "要不要看零余额"是展示偏好，属于前端；
    后端过滤掉会让"零余额"和"这个币根本没查"变得无法区分。

    不含任何估值字段：这一层的输入只有链，没有任何价格信息。
    """
    return chain_service.read_with_failover(
        chain, lambda w3: _read_holdings_on_node(w3, chain, address, symbols), resources=resources
    )


def _read_holdings_on_node(
    w3: Web3, chain: ChainMeta, address: str, symbols: tuple[str, ...] | None = None
) -> list[Asset]:
    """在一个已验证链身份的节点读取整组余额。

    先固定区块高度，再分别读取原生币与 ERC-20，保证同次查询使用同一区块。
    单个 ERC-20 读取失败保留 amount=None，成功读到零保留 Decimal(0)。
    """
    tokens = select_tokens(chain.chain_id, symbols)

    block = w3.eth.block_number
    assets: list[Asset] = []

    # 原生币：唯一不走 Multicall 的条目（它没有合约，调不出 balanceOf）
    native = native_token(tokens)
    if native is not None:
        assets.append(
            Asset(
                symbol=native.symbol,
                contract=None,
                amount=to_human(chain_service.read_native_balance(w3, address, block), native.decimals),
                decimals=native.decimals,
                kind="native",
            )
        )

    # 只查原生 BERA 时不触发任何 ERC-20 读取，WBERA 则只走其独立合约。
    balances = (
        chain_service.read_erc20_balances(w3, chain, tokens, address, block) if erc20_tokens(tokens) else {}
    )
    for meta in erc20_tokens(tokens):
        assert meta.address is not None  # erc20_tokens 已过滤，此处仅是类型收窄
        assets.append(
            Asset(
                symbol=meta.symbol,
                contract=Web3.to_checksum_address(meta.address),
                # 失败与零余额严格区分。
                amount=(
                    to_human(balances[meta.address.lower()], meta.decimals)
                    if meta.address.lower() in balances
                    else None
                ),
                decimals=meta.decimals,
                kind="erc20",
            )
        )
    return assets


async def _load_quotes(
    chain: ChainMeta, *, resources: BusinessResources | None = None
) -> dict[str, TokenMarket]:
    """取该链行情。失败返回空表，**不向上抛**。

    上游行情源全挂（且没有可降级的缓存）时 `market_service` 会抛 UpstreamError，
    这里必须收掉：查资产这个动作本身并没有失败，失败的只是"顺便算个价"。
    让它冒上去会变成一次 502 —— 用户看到的是"查不到我的资产"，
    而事实上余额一条不差地读到了。
    """
    try:
        result = await market_service.get_quotes(chain.chain_id, resources=resources)
    except UpstreamError as exc:
        logger.warning("行情不可用，本次只返回余额：%s", exc)
        return {}
    except Exception as exc:
        # 行情客户端初始化或解析异常也不能丢掉独立读取的余额；日志保留错误类型，
        # 对外仍由估值层生成缺价标识，不泄漏代理、节点地址或误填价格为零。
        logger.warning("行情读取异常，本次只返回余额：%s", type(exc).__name__)
        return {}
    return {quote.symbol: quote for quote in result.tokens}


@singleflight
async def get_wallet_assets(
    chain_id: int,
    owner: str,
    symbols: tuple[str, ...] | None = None,
    include_valuation: bool = True,
    *,
    resources: BusinessResources | None = None,
) -> WalletAssets:
    """按明确币种读取；None 保留普通资产 API 的全清单行为。

    symbols 使用可哈希 tuple，参与 singleflight 键，不会把 BERA 查询与全量查询合并。
    仅余额请求不访问行情，不产生“缺价”问题，也不被无关币种合约故障阻塞。
    """
    chain = get_chain(chain_id)
    address = to_checksum(owner)
    select_tokens(chain_id, symbols)  # 在访问外部数据源前拒绝不支持的币种。

    # 两件事互不依赖，并发做。当前使用的 Web3 HTTPProvider 是同步阻塞的，
    # 丢线程池执行 —— 直接在 async 函数里调用会卡住事件循环，并发请求一起排队。
    if include_valuation:
        quotes, holdings = await asyncio.gather(
            _load_quotes(chain, resources=resources),
            run_in_threadpool(lambda: _read_holdings(chain, address, symbols, resources=resources)),
        )
    else:
        quotes = {}
        holdings = await run_in_threadpool(
            lambda: _read_holdings(chain, address, symbols, resources=resources)
        )

    valuation = value_assets(holdings, quotes)

    issues = [
        DataIssue(code="balance_unavailable", asset=a.symbol, message=f"{a.symbol} 余额读取失败")
        for a in holdings
        if a.amount is None
    ]
    issues += [
        DataIssue(code="price_unavailable", asset=s, message=f"{s} 持仓缺少报价")
        for s in valuation.missing_price
        if include_valuation
    ]
    return WalletAssets(
        status="partial" if issues else "complete",
        issues=issues,
        address=address,
        chain_id=chain.chain_id,
        chain_name=chain.name,
        explorer=chain.explorer,
        assets=valuation.assets,
        total_value_usd=valuation.total_value_usd,
        has_valuation=valuation.has_valuation,
        missing_price=valuation.missing_price if include_valuation else [],
        stale=valuation.stale,
        computed_at=datetime.now(UTC),
    )
