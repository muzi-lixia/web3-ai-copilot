"""知识库文档管理接口。删改文档只对白名单地址开放，检索对所有登录用户开放。"""

from typing import Annotated

from fastapi import APIRouter, File, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import AdminIdentity, CurrentIdentity
from app.api.responses import success
from app.core.exceptions import PayloadTooLargeError

router = APIRouter(prefix="/knowledge", tags=["知识库"])

# 单次读取的块大小：边读边计量，超限立刻停手，不把整个文件读进内存。
CHUNK_BYTES = 1024 * 1024


class SearchQuery(BaseModel):
    """检索请求：一句自然语言问题，以及想要的片段条数。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)


async def read_limited(file: UploadFile, limit: int) -> bytes:
    """分块读取上传内容，累计超过上限时直接拒绝。"""
    parts: list[bytes] = []
    total = 0
    while chunk := await file.read(CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError("文件体积超过上传上限")
        parts.append(chunk)
    return b"".join(parts)


@router.post("/documents", status_code=201, summary="上传知识库文档")
async def upload(
    request: Request,
    admin: AdminIdentity,
    file: Annotated[UploadFile, File(description="PDF / Markdown / TXT / DOCX")],
):
    """接收原件并登记为待处理文档；解析与向量化由离线链路在后台完成。"""
    services = request.app.state.services
    content = await read_limited(file, services.settings.knowledge_max_bytes)
    return success(await services.knowledge.upload(file.filename or "", content))


@router.get("/documents", summary="列出知识库文档")
async def listing(request: Request, admin: AdminIdentity):
    """返回全部文档的元数据与处理状态，按上传时间倒序。"""
    return success(await request.app.state.services.knowledge.listing())


@router.get("/documents/{document_id}", summary="读取文档元数据")
async def detail(document_id: str, request: Request, admin: AdminIdentity):
    """按 id 返回单条文档元数据，含分片数量与失败原因。"""
    return success(await request.app.state.services.knowledge.read(document_id))


@router.post("/documents/{document_id}/retry", summary="重试失败的文档")
async def retry(document_id: str, request: Request, admin: AdminIdentity):
    """把处理失败的文档重新排进离线链路；原件仍在，不必重新上传。"""
    return success(await request.app.state.services.knowledge.retry(document_id))


@router.delete("/documents/{document_id}", status_code=204, summary="删除文档")
async def remove(document_id: str, request: Request, admin: AdminIdentity):
    """删除文档元数据与原文件，向量分片随外键级联删除。"""
    await request.app.state.services.knowledge.remove(document_id)
    return Response(status_code=204)


@router.post("/search", summary="检索知识库片段")
async def search(body: SearchQuery, request: Request, current: CurrentIdentity):
    """按语义检索已入库片段，返回原文与出处（文档名、标题路径、页码、相似度）。

    知识库是公共内容，任何登录用户都能检索；只有增删文档需要白名单。
    """
    return success(await request.app.state.services.knowledge.search(body.query, body.limit))
