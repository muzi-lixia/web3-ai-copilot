# contracts —— 自有锁仓合约工程

独立于 `backend/` 的 Solidity 工程。它与 Python 后端**唯一的接口是一份导出的 ABI**，没有构建期耦合——两边的依赖清单、工具链、CI 各自独立。

## 为什么单独放

| | `backend/` | `contracts/` |
|---|---|---|
| 语言 | Python | Solidity |
| 工具链 | uv / pytest / ruff | Foundry 或 Hardhat |
| 产物 | 运行中的 HTTP 服务 | 链上合约实例 + ABI 文件 |
| 变更频率 | 持续迭代 | 部署一次，很少动 |

后端读合约走 `backend/app/services/staking/custom.py`，它只加载本目录的 `abi/NodeStaking.json`，不引用 `src/`。合约源码放 `backend/` 里只会让两套东西共用忽略规则和依赖，是纯负担。

## 目录

    src/      Solidity 源码
    abi/      部署后导出的 ABI（backend 运行时读这个）
    scripts/  部署脚本
    tests/    合约测试

## 合约最小接口

够演示就行，不写复杂逻辑：

```solidity
struct Position {
    uint256 amount;
    uint256 stakedAt;    // 收益计算的依据
    uint256 unlockTime;
    uint256 apyBps;      // 基点，500 = 5%
}

function stake(uint256 amount, uint256 lockDays) external;
function unstake(uint256 index) external;
function pendingReward(address who, uint256 index) external view returns (uint256);
function getPositions(address who) external view returns (Position[] memory);
```

## 为什么这块反而比 Lido 那类更"真实"

| 字段 | Lido 那类（读标准 ERC-20） | 本合约 |
|---|---|---|
| 仓位数量 | 链上可读 | 链上可读 |
| APY | 要调协议外部 API | 合约里直接存了 |
| 收益 | 只能按 APY 推算 | **可精确计算**（合约存了 `stakedAt`） |
| 解锁时间 | 只能给机制说明 | **真实时间戳** |

## 待定

工具链还没定（Foundry / Hardhat 二选一）。无论选哪个，**部署后必须把 ABI 导出到 `abi/NodeStaking.json`**——这是 backend 侧能读到它的唯一前提。
