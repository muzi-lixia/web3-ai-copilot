/**
 * 与后端 `app/schemas/` 对齐的类型定义。
 *
 * V1 手工维护；V2 起可用 openapi-typescript 从 `/api/v1/openapi.json` 生成，避免手工漂移。
 */

/** 后端统一错误响应体，见 backend/app/core/errors.py */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
  }
}

export interface HealthResponse {
  status: string
  chain_id: number
}
