#!/usr/bin/env python
"""核对常量表里的合约地址与链上实际返回值是否一致。

用法（必须在 backend/ 目录下）：
    .venv/bin/python scripts/verify_constants.py

为什么需要它：`constants/tokens.py` 里的地址和 decimals 是手写的。
写错**不会报错** —— 它只会静默地读到一个不存在的合约（返回空），
或者读到一个完全不相干的合约。后者更糟：会给出看起来合理但实际错误的数据。
所以每个地址入库前都要用链上真值核对一次：

    symbol()    —— 地址是谁
    decimals()  —— 归一化参数对不对（错一位，金额差几个数量级）

检查项：
    1. 每个地址是合法且 checksum 格式正确（纯本地，不需要网络）
    2. symbol() / decimals() 与表内声明一致（逐链）。**名称允许登记别名** ——
       `TokenMeta.onchain_symbol` 记的是"合约自称的名字"，与展示名不同时填它
       （实测两处：USD₮0、BUSD）。没登记的差异一律报错，判据不放松
    3. 节点返回的 chain_id 与目标链一致
    4. 行情源的链标识没写错（见 check_market —— 写错的症状和"这币没有市场"
       长得一模一样，光看接口输出分不出来）
    5. 质押模块：金库的 symbol/decimals、底层资产、**链上解绑时长**与注册表声明一致；
       并实打一次 Beep 的年化接口（见 check_staking）
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web3 import Web3  # noqa: E402

from app.infrastructure.blockchain import chains as C  # noqa: E402
from app.infrastructure.blockchain.client import connect  # noqa: E402
from app.infrastructure.providers import beep
from app.modules.asset import tokens as T  # noqa: E402
from app.modules.market import service as market_service  # noqa: E402
from app.modules.staking import constants as S  # noqa: E402

READ_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


STAKING_ABI = [
    {
        "inputs": [],
        "name": "asset",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "WITHDRAWAL_COOLDOWN",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def short(address: str) -> str:
    """缩写地址仅用于终端展示；不会把缩写后的字符串用于链上请求。"""
    return f"{address[:10]}…{address[-6:]}"


def check_local() -> list[str]:
    """纯本地检查：地址格式与 checksum。不联网，所以放最前面先跑。"""
    problems: list[str] = []

    for chain in C.CHAINS:
        for meta in T.tokens_for(chain.chain_id):
            if meta.address is None:
                continue  # 原生币没有合约地址
            try:
                Web3.to_checksum_address(meta.address)
            except Exception:  # noqa: BLE001
                problems.append(f"[{chain.key}] {meta.symbol}: 地址非法 {meta.address}")
                continue
            if not Web3.is_checksum_address(meta.address):
                problems.append(
                    f"[{chain.key}] {meta.symbol}: checksum 格式不正确（应为混合大小写）{meta.address}"
                )

    return problems


def read_erc20(w3: Web3, address: str) -> tuple[str | None, int | None, str | None]:
    """返回 (链上 symbol, 链上 decimals, 错误)。错误非空表示读取失败。"""
    checksum = Web3.to_checksum_address(address)

    # 先确认地址上确实有合约。否则 symbol() 只会抛一个含糊的
    # BadFunctionCallOutput，掩盖「地址根本不存在」这个真实原因
    # —— 而地址写错正是这张表最主要的失败模式。
    try:
        if not w3.eth.get_code(checksum):
            return None, None, "该地址没有合约代码（地址错误或未部署）"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"读取合约代码失败：{type(exc).__name__}"

    contract = w3.eth.contract(address=checksum, abi=READ_ABI)
    try:
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()
    except Exception as exc:  # noqa: BLE001
        return None, None, type(exc).__name__
    # 少数老 token 的 symbol() 声明为 bytes32，web3 会返回 bytes
    if isinstance(symbol, bytes):
        symbol = symbol.rstrip(b"\x00").decode("utf-8", "replace")
    return str(symbol), int(decimals), None


def verify_chain(chain: C.ChainMeta) -> list[str]:
    """核对一条链。返回该链的问题列表（空 = 全过）。"""
    print(f"\n══ {chain.name}（chain_id={chain.chain_id}）══════════════════════")

    tokens = T.tokens_for(chain.chain_id)
    erc20s = T.erc20_tokens(tokens)
    native = T.native_token(tokens)

    try:
        w3 = connect(chain)
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] 节点不可用：{exc}")
        return [f"[{chain.key}] 无可用节点"]

    print(f"  节点 chain_id={w3.eth.chain_id}  block={w3.eth.block_number}")
    if native is None:
        print("  [!!] 清单里没有原生币条目（address=None 的那条）")
    else:
        print(f"  原生币 {native.symbol} decimals={native.decimals}（无合约可核，仅提示）")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda m: (m, *read_erc20(w3, m.address)), erc20s)  # type: ignore[arg-type]
        )

    problems: list[str] = []
    for meta, actual_symbol, actual_decimals, error in results:
        if error:
            problems.append(f"[{chain.key}] {meta.symbol}: 读取失败 —— {error}")
            print(f"  [!!] {meta.symbol:<9} {short(meta.address or '')}  读取失败：{error}")
            continue
        # 登记了 `onchain_symbol` 的，拿**登记值**去比。只有 USDT0 / HONEY 两处：
        # 链上写法（USD₮0、BUSD）与展示名不同是已核实的事实，不是地址错。
        # 没登记的仍然按 `symbol` 比 —— 判据一点没放松，"读到了别个合约"照样报错。
        expected_symbol = meta.onchain_symbol or meta.symbol
        if actual_symbol != expected_symbol or actual_decimals != meta.decimals:
            problems.append(
                f"[{chain.key}] {meta.symbol}: 表内 {expected_symbol}/{meta.decimals} "
                f"≠ 链上 {actual_symbol}/{actual_decimals}"
            )
            print(
                f"  [!!] {meta.symbol:<9} {short(meta.address or '')}  "
                f"表内 {expected_symbol}/{meta.decimals}  ≠  链上 {actual_symbol}/{actual_decimals}"
            )
            continue
        print(
            f"  [ok] {meta.symbol:<9} {short(meta.address or '')}  "
            f"symbol={actual_symbol:<9} decimals={actual_decimals}"
        )

    print(f"  —— {len(erc20s)} 个 ERC-20，通过 {len(erc20s) - len(problems)}，异常 {len(problems)}")
    return problems


def check_market(chain: C.ChainMeta) -> list[str]:
    """核对行情源：清单里哪些 token 真能取到价。

    `dexscreener_id` / `defillama_id` 和合约地址一样是手写常量，写错**不会报错**
    —— 只是清单里所有币一起取不到价，而接口那边仅仅把它们列进 `missing`，
    与"这币本来就没有市场"（目前只有 BVT 一个 —— 它没有任何交易池）看起来完全一样。
    所以这里把两种情况分开：**一个价都取不到 = 链标识大概率写错了**；
    取到一部分 = 标识是对的，剩下的就是真的没有市场。
    """
    print(f"\n── 行情源（{chain.name}）────────────────────────────")
    try:
        result = asyncio.run(market_service.get_quotes(chain.chain_id))
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] 行情源不可用：{type(exc).__name__}: {exc}")
        return [f"[{chain.key}] 行情源不可用（检查网络或 MARKET_PROXY）"]

    for token in result.tokens:
        # 按源而不是按字段判空：DefiLlama 本来就不提供涨跌/市值/成交量，
        # 标出来免得被当成"取数缺失"。用 change_24h 判空是不准的 ——
        # DexScreener 个别浅池子的 priceChange 里也可能没有 h24。
        extra = "（该源不提供涨跌/市值/成交量）" if token.source == "defillama" else ""
        print(f"  [ok] {token.symbol:<9} {token.source:<12} price={token.price_usd}{extra}")
    for symbol in result.missing:
        print(f"  [--] {symbol:<9} 所有源都没有报价")

    print(f"  —— {len(result.tokens)} 个有价，{len(result.missing)} 个无价")
    if not result.tokens:
        return [f"[{chain.key}] 行情一个价都取不到，检查 dexscreener_id / defillama_id 是否写错"]
    return []


def check_staking(chain: C.ChainMeta) -> list[str]:
    """核对质押注册表：金库是谁、底层是谁、解绑多久、年化打不打得到。

    与 tokens 那边同样的道理，这四样**没有一个会"写错就报错"**：

        金库地址写错         → 读到空，界面显示"没有质押仓位"，与真的没质押一样
        share_decimals 写错  → 份额数量差几个数量级
        unbonding_seconds    → "还有几天能提"整个算错，而日期看起来照样合理
        Beep 打不通          → 年化恒为 null，看着像"这个金库不提供年化"
    """
    modules = S.modules_for(chain.chain_id)
    if not modules:
        return []

    print(f"\n── 质押模块（{chain.name}）──────────────────────────")
    try:
        w3 = connect(chain)
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] 节点不可用：{exc}")
        return [f"[{chain.key}] 质押核对时节点不可用"]

    problems: list[str] = []
    by_address = {t.address.lower(): t for t in T.tokens_for(chain.chain_id) if t.address}

    for module in modules:
        actual_symbol, actual_decimals, error = read_erc20(w3, module.vault)
        if error:
            problems.append(f"[{chain.key}] 质押 {module.key}: 金库读取失败 —— {error}")
            print(f"  [!!] {module.key:<12} 金库 {short(module.vault)} 读取失败：{error}")
            continue

        if actual_symbol != module.share_symbol or actual_decimals != module.share_decimals:
            problems.append(
                f"[{chain.key}] 质押 {module.key}: 表内 {module.share_symbol}/{module.share_decimals} "
                f"≠ 链上 {actual_symbol}/{actual_decimals}"
            )
            print(
                f"  [!!] {module.key:<12} 表内 {module.share_symbol}/{module.share_decimals}"
                f"  ≠  链上 {actual_symbol}/{actual_decimals}"
            )
        else:
            print(f"  [ok] {module.key:<12} 份额 {actual_symbol} decimals={actual_decimals}")

        vault = w3.eth.contract(address=Web3.to_checksum_address(module.vault), abi=STAKING_ABI)

        # 底层资产：链上的 asset() 才是事实，注册表里声明的 symbol 只是我们的注解。
        # 两者不一致时服务层按链上的走并打警告，这里把它提前暴露成一条明确的问题。
        try:
            underlying = Web3.to_checksum_address(vault.functions.asset().call())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"[{chain.key}] 质押 {module.key}: asset() 读取失败 —— {type(exc).__name__}")
            print(f"  [!!] {module.key:<12} asset() 读取失败：{type(exc).__name__}")
        else:
            token = by_address.get(underlying.lower())
            if token is None:
                problems.append(
                    f"[{chain.key}] 质押 {module.key}: 链上底层 {underlying} 不在 token 清单里，"
                    "估值永远算不出来"
                )
                print(f"  [!!] {module.key:<12} 底层 {underlying} 不在 token 清单里")
            elif token.symbol != module.underlying_symbol:
                problems.append(
                    f"[{chain.key}] 质押 {module.key}: 注册表声明底层 {module.underlying_symbol}，"
                    f"链上是 {token.symbol}"
                )
                print(f"  [!!] {module.key:<12} 声明 {module.underlying_symbol}  ≠  链上 {token.symbol}")
            else:
                print(f"  [ok] {module.key:<12} 底层 {token.symbol}（{short(underlying)}）")

        try:
            cooldown = int(vault.functions.WITHDRAWAL_COOLDOWN().call())
        except Exception as exc:  # noqa: BLE001
            # 不是失败：服务层会回退到注册表声明值。但要说出来，
            # 否则"回退"是静默的，声明值错了也不会有人发现。
            print(
                f"  [--] {module.key:<12} WITHDRAWAL_COOLDOWN() 读不到"
                f"（{type(exc).__name__}），运行时回退到注册表声明值"
            )
        else:
            # 读到了才比对。写成 elif 是因为脑子里想的是 if/elif/else，
            # 但这里是 try —— 读不到走 except，读到了才进这一支。
            if cooldown != module.unbonding_seconds:
                problems.append(
                    f"[{chain.key}] 质押 {module.key}: 表内解绑 {module.unbonding_seconds}s "
                    f"≠ 链上 {cooldown}s"
                )
                print(f"  [!!] {module.key:<12} 表内 {module.unbonding_seconds}s ≠ 链上 {cooldown}s")
            else:
                print(f"  [ok] {module.key:<12} 解绑期 {cooldown}s（{cooldown / 86400:.0f} 天）")

    # Beep 是年化与收益的唯一来源。取不到不影响上面的核对，但必须显式说出来 ——
    # 否则"年化一直是 null"会被读成"这个金库不提供年化"。
    try:
        apy = asyncio.run(beep.get_stake_apy())
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] Beep 年化取不到：{exc}")
        print("       检查网络 / 代理，或 BEEP_BASE_URL / BEEP_CLIENT_ID 配置")
        problems.append(f"[{chain.key}] 质押年化取不到（Beep 不可用）")
    else:
        print(f"  [ok] Beep 年化 {apy.interval} = {apy.apy * 100:.2f}%")

    return problems


def main() -> int:
    """先核对本地地址格式，再逐链验证代币、行情和质押配置。

    汇总所有异常后以退出码 1 表示失败，便于维护人员或自动化脚本发现常量漂移。
    """
    local = check_local()
    print("── 本地检查（地址格式 / checksum）────────────────────")
    if local:
        for item in local:
            print(f"  [!!] {item}")
    else:
        print("  [ok] 全部通过")

    problems = list(local)
    for chain in C.CHAINS:
        problems.extend(verify_chain(chain))
        problems.extend(check_market(chain))
        problems.extend(check_staking(chain))

    print("\n── 结论 ──────────────────────────────────────────────")
    if problems:
        print(f"共 {len(problems)} 处异常，修正后再提交。")
        return 1
    print("全部一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
