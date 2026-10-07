"""钱包登录路由。

流程：
    POST /auth/nonce    {address}                → {nonce, message, expires_at}
    前端用钱包对 message 签名
    POST /auth/verify   {address, signature}     → {token, address, expires_at}
    GET  /auth/me       Authorization: Bearer …  → {address}
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_address
from app.schemas.auth import MeResponse, NonceRequest, NonceResponse, TokenResponse, VerifyRequest
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/nonce", response_model=NonceResponse, summary="获取签名随机数")
async def post_nonce(payload: NonceRequest) -> NonceResponse:
    nonce, message, expires_at = auth_service.issue_nonce(payload.address)
    return NonceResponse(
        address=payload.address,
        nonce=nonce,
        message=message,
        expires_at=expires_at,
    )


@router.post("/verify", response_model=TokenResponse, summary="提交签名换取登录凭证")
async def post_verify(payload: VerifyRequest) -> TokenResponse:
    address = auth_service.verify_login(payload.address, payload.signature)
    token, expires_at = auth_service.create_token(address)
    return TokenResponse(token=token, address=address, expires_at=expires_at)


@router.get("/me", response_model=MeResponse, summary="校验登录凭证并返回当前地址")
async def get_me(address: str = Depends(get_current_address)) -> MeResponse:
    return MeResponse(address=address)
