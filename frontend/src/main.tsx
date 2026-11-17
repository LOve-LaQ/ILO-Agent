import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import './styles/global.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('未找到 #root 挂载点');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// 仅生产构建注册 Service Worker（见 public/sw.js 的取舍说明）。
// 开发环境不注册，避免 SW 生命周期干扰 HMR：改了代码刷新却不生效。
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => {
      // 注册失败（如非 HTTPS）不影响任何功能，静默降级为普通网页
    });
  });
}
