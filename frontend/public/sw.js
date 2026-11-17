/*
 * 极简 Service Worker —— 只为让浏览器认为本应用「可安装」。
 *
 * 【为什么不做缓存】离线可用需要接管所有静态资源与版本失效策略；这套项目
 * 是演示级单机部署，缓存一旦和部署版本错配，排查成本远高于收益（用户看到
 * 的是「改了代码但页面不变」）。所以这里刻意只做一件事：存在 fetch 事件处理器。
 *
 * 【为什么只接管导航】API 请求、下载、以及未来可能加的流式响应一律交回网络，
 * 避免 respondWith 包装带来的缓冲/中断风险。
 */

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request));
  }
});
