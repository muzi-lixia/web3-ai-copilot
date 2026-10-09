"""认证资源：创建一次性挑战、读取挑战、签名换令牌及读取当前用户。"""

from fastapi import APIRouter, Depends, Path, Request, Response

from app.api.dependencies import AuthServiceDep, get_current_address
from app.api.responses import success
from app.common.schemas import ApiResponse
from app.modules.auth.schemas import MeResponse, NonceRequest, NonceResponse, TokenResponse, VerifyRequest

router = APIRouter(tags=["auth"])


async def limit_login(request: Request, auth_service: AuthServiceDep) -> None:
    """创建与查询挑战、签发令牌共用来源限流，不信任任意客户端转发头。"""
    auth_service.check_auth_rate(request.client.host if request.client else "unknown")


@router.post(
    "/auth/challenges",
    dependencies=[Depends(limit_login)],
    status_code=201,
    response_model=ApiResponse[NonceResponse],
)
async def create_challenge(
    payload: NonceRequest, request: Request, response: Response, auth_service: AuthServiceDep
):
    """创建挑战资源，201 与 Location 指向随机挑战；同钱包新挑战使旧挑战失效。"""
    nonce, message, expires_at = auth_service.issue_nonce(payload.address)
    location = str(request.url_for("get_challenge", nonce=nonce))
    response.headers["Location"] = location
    return success(
        NonceResponse(address=payload.address, nonce=nonce, message=message, expires_at=expires_at)
    )


@router.get(
    "/auth/challenges/{nonce}",
    dependencies=[Depends(limit_login)],
    response_model=ApiResponse[NonceResponse],
)
async def get_challenge(
    auth_service: AuthServiceDep,
    nonce: str = Path(pattern=r"^[0-9a-f]{32}$"),
):
    """读取未过期挑战；挑战 ID 仅用于读取原文，不能代替钱包签名。"""
    return success(auth_service.read_challenge(nonce))


@router.post("/auth/tokens", dependencies=[Depends(limit_login)], response_model=ApiResponse[TokenResponse])
async def create_token(payload: VerifyRequest, auth_service: AuthServiceDep):
    """消费挑战并验证签名后签发令牌；POST 返回 200 表示凭据处理成功。"""
    address = auth_service.verify_login(payload.address, payload.signature)
    token, expires_at = auth_service.create_token(address)
    return success(TokenResponse(token=token, address=address, expires_at=expires_at))


@router.get("/users/me", response_model=ApiResponse[MeResponse])
async def get_current_user(address: str = Depends(get_current_address)):
    """当前用户资源，身份来自 JWT，不由查询参数决定。"""
    return success(MeResponse(address=address))
