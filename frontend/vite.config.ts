import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  server: {
    // 后端为本地 FastAPI（uvicorn 默认 127.0.0.1:8000）。开发期经代理转发，
    // 前端一律以同源相对路径 /api/... 访问，避免 CORS 与硬编码主机名（规格 §4.1）。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: true,
  },
})
