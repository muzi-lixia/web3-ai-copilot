"""ASGI 入口与应用工厂；每个应用实例拥有独立服务及资源，支持测试注入。"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import get_settings
from app.core.container import ApplicationServices
from app.core.exception_handlers import error_responses, register_exception_handlers
from app.core.logging import setup_logging


def create_app(services: ApplicationServices | None = None) -> FastAPI:
    """创建并装配 FastAPI 应用。

    services 可由测试或调用方注入；未传入时按配置建立独立容器。
    工厂只完成对象创建和路由注册，数据库运行锁在 lifespan 启动阶段取得。
    服务放在 app.state 中，请求依赖从当前应用读取，避免多个应用共享聊天状态。
    """
    # 每个应用工厂拥有独立运行状态；注入容器便于测试替换模型和数据库。
    services = services or ApplicationServices.build(get_settings())
    settings = services.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """管理应用启动与关闭的完整顺序。

        启动时先取得单实例运行锁并收尾旧 running，再接受 HTTP 请求。
        关闭时先等待聊天任务保存状态、释放租约和数据库资源，最后停止鉴权清理任务。
        两层 finally 保证前一步抛异常时仍会执行后续资源清理。
        """
        setup_logging("DEBUG" if settings.debug else "INFO")

        async def cleanup():
            """每分钟回收过期 nonce 和登录限流条目。

            这是进程内维护任务，不是持久化 Worker；应用关闭时会取消并等待它退出。
            """
            while True:
                # 每分钟回收过期的登录nonce和限流条目
                services.auth.cleanup_auth_state()
                await asyncio.sleep(60)

        task = None
        try:
            # 启动聊天运行时
            await services.chat.start()
            # 创建后台清理任务
            task = asyncio.create_task(cleanup())
            yield
        finally:
            try:
                # 关闭应用时先等待聊天任务保存状态并释放资源
                await services.close()
            # 取消并等待清理任务完成
            finally:
                if task is not None:
                    task.cancel()  # 取消清理任务
                    with suppress(asyncio.CancelledError):
                        await task  # 确保任务完全退出

    application = FastAPI(
        lifespan=lifespan,
        responses=error_responses(),
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    # 依赖解析以请求所属应用为准，避免引用另一个应用的全局聊天实例。
    application.state.services = services  # 服务实例绑定到 app.state
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Location", "WWW-Authenticate"],
    )

    # 注册异常处理器
    @application.middleware("http")
    async def private_response_headers(request: Request, call_next):
        """资源包含钱包身份、凭据或聊天正文，禁止浏览器和代理缓存；业务内部缓存不受影响。"""
        response = await call_next(request)
        if request.url.path.startswith(settings.api_prefix + "/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    register_exception_handlers(application)
    application.include_router(api_router, prefix=settings.api_prefix)
    application.include_router(health_router)

    return application


app = create_app()
