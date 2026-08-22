import { defineConfig } from 'vite';
import { resolve } from 'path';

// MPA：index(首页) / edit(编辑会话) / drawing(图纸对照) / report(报告中心)。
// 开发模式 vite dev (5173) 代理 API 到本地 cad_service (8764)。
// 生产模式 vite build -> dist/，由 cad_service 在 /app/ 路径下静态服务（同源）。
export default defineConfig({
  base: '/app/',
  server: {
    port: 5173,
    proxy: {
      '/api':     'http://127.0.0.1:8764',
      '/cache':   'http://127.0.0.1:8764',
      '/versions': 'http://127.0.0.1:8764',
      '/drafts':  'http://127.0.0.1:8764',
      '/drawings': 'http://127.0.0.1:8764',
      '/fea':     'http://127.0.0.1:8764',
      '/render':  'http://127.0.0.1:8764',
      '/ws':    { target: 'ws://127.0.0.1:8764', ws: true },
    },
  },
  build: {
    rollupOptions: {
      input: {
        index:   resolve(__dirname, 'index.html'),
        edit:    resolve(__dirname, 'edit.html'),
        drawing: resolve(__dirname, 'drawing.html'),
        report:  resolve(__dirname, 'report.html'),
      },
    },
  },
});
