/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Reown projectId。缺了钱包弹窗打不开，见 src/config/wallet.ts。 */
  readonly VITE_REOWN_PROJECT_ID?: string
  /** 后端地址。不填则走 vite proxy 到 127.0.0.1:8000。 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
