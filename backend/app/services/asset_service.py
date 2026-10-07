"""钱包资产查询：把链上余额组装成前端契约。

分两步，两步的**失败语义完全不同**，所以分开写也分开处理：

    1. 读余额（`_read_holdings`）—— 走自家 RPC，失败就是失败，向上抛
    2. 读行情（`_load_quotes`）  —— 走第三方行情源，失败降级为空表，不抛

第二步失败不能让第一步的结果消失。"资产"这件事的主体是余额，
估值只是加在它上面的一层注解 —— 注解没盖章，正文照样要交。
"""

import asyncio
from datetime import UTC, datetime

from fastapi.concurrency import run_in_threadpool
from web3 import Web3

from app.api.schemas.common import DataIssue
from app.api.schemas.market import TokenMarket
from app.api.schemas.wallet import Asset, WalletAssets
from app.config.constants.chains import ChainMeta, get_chain
from app.config.constants.tokens import erc20_tokens, native_token, tokens_for
from app.infra.blockchain import chain as chain_service
from app.infra.blockchain.chain import to_checksum
from app.infra.logging import get_logger
from app.infra.singleflight import singleflight
from app.services import market_service
from app.services.units import to_human
from app.services.valuation import value_assets
from app.shared.errors import UpstreamError

logger = get_logger(__name__)


def _read_holdings(chain: ChainMeta, address: str) -> list[Asset]:
    """链上读取（同步阻塞）。返回**全部候选条目**，含余额为 0 的。

    不在这里过滤零余额 —— "要不要看零余额"是展示偏好，属于前端；
    后端过滤掉会让"零余额"和"这个币根本没查"变得无法区分。

    不含任何估值字段：这一层的输入只有链，没有任何价格信息。
    """
    return chain_service.read_with_failover(chain, lambda w3: _read_holdings_on_node(w3, chain, address))


def _read_holdings_on_node(w3: Web3, chain: ChainMeta, address: str) -> list[Asset]:
    tokens = tokens_for(chain.chain_id)

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

    balances = chain_service.read_erc20_balances(w3, chain, tokens, address, block)
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


async def _load_quotes(chain: ChainMeta) -> dict[str, TokenMarket]:
    """取该链行情。失败返回空表，**不向上抛**。

    上游行情源全挂（且没有可降级的缓存）时 `market_service` 会抛 UpstreamError，
    这里必须收掉：查资产这个动作本身并没有失败，失败的只是"顺便算个价"。
    让它冒上去会变成一次 502 —— 用户看到的是"查不到我的资产"，
    而事实上余额一条不差地读到了。
    """
    try:
        result = await market_service.get_quotes(chain.chain_id)
    except UpstreamError as exc:
        logger.warning("行情不可用，本次只返回余额：%s", exc)
        return {}
    return {quote.symbol: quote for quote in result.tokens}


@singleflight
async def get_wallet_assets(chain_id: int, owner: str) -> WalletAssets:
    """查询 owner 在 chain_id 上的资产，并尽可能附上 USD 估值。"""
    chain = get_chain(chain_id)
    address = to_checksum(owner)

    # 两件事互不依赖，并发做。链上读取是同步阻塞的（web3.py 没有异步 provider），
    # 丢线程池执行 —— 直接在 async 函数里调用会卡住事件循环，并发请求一起排队。
    quotes, holdings = await asyncio.gather(
        _load_quotes(chain),
        run_in_threadpool(_read_holdings, chain, address),
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
        missing_price=valuation.missing_price,
        stale=valuation.stale,
        computed_at=datetime.now(UTC),
    )
