/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API 基础前缀，开发环境走 vite proxy 的同源 /api/v1 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module '*.module.css' {
  const classes: Record<string, string>;
  export default classes;
}
