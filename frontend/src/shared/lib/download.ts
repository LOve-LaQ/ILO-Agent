/**
 * 浏览器端文件下载工具（shared 层）
 */

/**
 * 把任意可序列化对象下载成 JSON 文件。
 *
 * 【为什么在浏览器端拼文件】后端 `/me/export` 以 `Content-Disposition: attachment`
 * 返回 JSON，但前端用统一的 api 客户端取到时已经是解析好的对象（fetch 不自动触发下载）。
 * 这里把它重新序列化并触发一次 `<a download>` 点击，效果与直接访问下载链接一致，
 * 同时还能复用客户端的 token 注入与 401 刷新逻辑。
 */
export function downloadJsonFile(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
