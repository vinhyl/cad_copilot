import { defineConfig } from 'vite';

// 开发模式：vite dev (5173) 代理 API 到本地 cad_service (8764)。
// 生产模式：vite build -> dist/，由 cad_service 在 /app/ 路径下静态服务（同源）。
export default defineConfig({
  base: '/app/',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8764',
      '/cache': 'http://127.0.0.1:8764',
      '/ws': { target: 'ws://127.0.0.1:8764', ws: true },
    },
  },
});
