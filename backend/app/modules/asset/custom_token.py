"""清单外 ERC20 读取：地址仅定位资产，钱包目标始终来自已验证身份。"""

import re
from decimal import localcontext

from anyio.to_thread import run_sync
from web3 import Web3

from app.core.exceptions import NotFoundError
from app.infrastructure.blockchain.chains import get_chain
from app.infrastructure.blockchain.client import read_with_failover
from app.modules.asset.schemas import Asset

ABI = [
    {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": inputs,
        "outputs": [{"type": output}],
    }
    for name, inputs, output in (
        ("decimals", [], "uint8"),
        ("symbol", [], "string"),
        ("balanceOf", [{"name": "owner", "type": "address"}], "uint256"),
    )
]


async def metadata(chain_id, contract, resources):
    """校验代码及 ERC20 只读方法；未通过的地址不能以“合约地址”名义进入模型。"""
    checksum = Web3.to_checksum_address(contract)
    if contract != contract.lower() and contract != contract.upper() and contract != checksum:
        raise NotFoundError("合约 checksum 不正确")

    def read(w3):
        if not w3.eth.get_code(checksum):
            raise NotFoundError("指定地址不是代币合约")
        token = w3.eth.contract(address=checksum, abi=ABI)
        decimals = token.functions.decimals().call()
        symbol = token.functions.symbol().call()
        if (
            not isinstance(decimals, int)
            or not 0 <= decimals <= 36
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", symbol)
        ):
            raise NotFoundError("代币元数据不在可安全展示范围内")
        return dict(contract=checksum, decimals=decimals, symbol=symbol, chain_id=chain_id)

    return await run_sync(lambda: read_with_failover(get_chain(chain_id), read, resources=resources))


async def balance(chain_id, contract, wallet, resources):
    meta = await metadata(chain_id, contract, resources)

    def read(w3):
        token = w3.eth.contract(address=meta["contract"], abi=ABI)
        return token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()

    raw = await run_sync(lambda: read_with_failover(get_chain(chain_id), read, resources=resources))
    with localcontext() as ctx:
        ctx.prec = 100
        from decimal import Decimal

        amount = Decimal(raw) / Decimal(10) ** meta["decimals"]
    return Asset(
        symbol=meta["symbol"],
        contract=meta["contract"],
        amount=amount,
        decimals=meta["decimals"],
        kind="erc20",
    )
