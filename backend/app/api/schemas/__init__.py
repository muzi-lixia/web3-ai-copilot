"""前后端契约唯一真源。

前端 `src/types/api.ts` 与这里一一对应：类型变更先改本目录，再同步前端。
"""

from app.api.schemas.agent import (
    AnalyzePortfolioRequest,
    AnalyzePortfolioResponse,
    ChatRequest,
)
from app.api.schemas.auth import MeResponse, NonceRequest, NonceResponse, TokenResponse, VerifyRequest
from app.api.schemas.common import Address, ErrorBody, ErrorResponse, Schema
from app.api.schemas.events import (
    AgentEvent,
    RunFailed,
    RunFinished,
    RunStarted,
    TokenDelta,
    ToolFinished,
    ToolStarted,
)
from app.api.schemas.market import TokenMarket, TokenMarketList
from app.api.schemas.risk import RiskReport
from app.api.schemas.staking import StakingPosition, StakingSummary
from app.api.schemas.wallet import Asset, WalletAssets

__all__ = [
    # common
    "Address",
    "ErrorBody",
    "ErrorResponse",
    "Schema",
    # auth
    "NonceRequest",
    "NonceResponse",
    "VerifyRequest",
    "TokenResponse",
    "MeResponse",
    # wallet
    "Asset",
    "WalletAssets",
    # market
    "TokenMarket",
    "TokenMarketList",
    # staking
    "StakingPosition",
    "StakingSummary",
    # risk
    "RiskReport",
    # events
    "AgentEvent",
    "RunStarted",
    "ToolStarted",
    "ToolFinished",
    "TokenDelta",
    "RunFinished",
    "RunFailed",
    # agent
    "ChatRequest",
    "AnalyzePortfolioRequest",
    "AnalyzePortfolioResponse",
]
