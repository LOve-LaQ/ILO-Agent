import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发期通过 proxy 走同源请求，避免依赖后端 CORS 通配配置
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
