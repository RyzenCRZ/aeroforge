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
      // 作业进度通道（§10.2 / §9.3）。漏掉这条时升级请求不被转发：浏览器会**静默停在
      // CONNECTING**（既不 open 也不 error/close），前端因此学不到作业状态、降级轮询也
      // 不会启动，界面卡在"排队中（0%）"——而作业其实早已在后端跑完。
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
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
