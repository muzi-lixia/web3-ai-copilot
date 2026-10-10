import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/foundation-openapi': { target: 'http://127.0.0.1:8000', rewrite: () => '/openapi.json' },
      '/agent-openapi': { target: 'http://127.0.0.1:8001', rewrite: () => '/openapi.json' },
      '/foundation-health': { target: 'http://127.0.0.1:8000', rewrite: () => '/health' },
      '/agent-health': { target: 'http://127.0.0.1:8001', rewrite: () => '/health' },
      // 开发期把 /api 转发到本地后端，前端代码里不需要写后端 host，也免了 CORS
      '/api/v1/chat': { target: 'http://127.0.0.1:8001', changeOrigin: true },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
