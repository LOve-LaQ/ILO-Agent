import { useCallback, useEffect, useId, useRef, useState } from 'react';

import styles from './Auth.module.css';

/**
 * 人机校验组件（VAPTCHA V4）。
 *
 * 【服务端才是真正的闸门】这里拿到的 token 只是「这个浏览器验证通过了」的证据，
 * 真伪由后端二次校验（backend/src/services/captcha_service.py）。所以本组件的职责
 * 只有一个：把 validate() 得到的校验材料交给表单，并在**拿不到结果时明确失败**——绝不静默放行。
 *
 * 【V4 的官方契约（已对照官方文档核实）】
 * - SDK：`https://c4.vaptcha.com/src/v4.js`，全局 `window.vaptcha(options)` 返回 Promise。
 * - `container` 只是**挂载锚点**，值用 CSS 选择器字符串；SDK **不会**往里面注入按钮/文案/点击事件，
 *   「什么时候发起验证」由业务页面决定。
 * - 发起验证 = 显式调用实例的 `validate()`（异步），其 resolve 值为 `{ token, knock, dfu, ip }`。
 * - V4 **没有** V3 时代的 `render()` / `getToken()` / `on()` / `listen('pass')`。
 *
 * 【为什么不让它在挂载时自动弹验证】V4 的挑战是以独立弹层呈现的（挂在 document.body 上），
 * 若在页面一加载就调用 validate()，用户还没填任何字段就会被一个全屏遮罩拦住——这既打扰用户，
 * 又会让「用户尚未表达意图」和「校验失败」混为一谈。因此这里改为**用户点击按钮才发起验证**。
 *
 * 【加载失败必须禁用提交】脚本加载失败、初始化失败、validate 拿不到 token 时，我们既没有凭证，
 * 也不该假装校验通过。此时回调 `onUnavailable(true)` 禁用提交按钮，这是「fail-closed」在前端的落点：
 * 宁可让用户重试/刷新，也不能放一个没有凭证的请求出去。
 *
 * 【脚本加载包成 Promise】onload/onerror 转 Promise，并做幂等（同一脚本只注入一次），
 * 避免 StrictMode 双挂载时重复注入、或并发页面各自注入一份。
 */

const SCRIPT_ID = 'vaptcha-v4-sdk';
const SCRIPT_SRC = 'https://c4.vaptcha.com/src/v4.js';

/** validate() 的成功返回（V4） */
interface VaptchaVerifyResult {
  token?: string;
  knock?: string;
  dfu?: string;
  ip?: string;
}

/** VAPTCHA V4 实例的可用方法（只约束我们要用的部分） */
interface VaptchaInstance {
  validate?: () => Promise<VaptchaVerifyResult | null | undefined>;
  reset?: () => void;
  listen?: (event: string, handler: (...args: unknown[]) => void) => void;
}

/** 交给表单的校验材料（token 必填，knock/dfu/ip 原样透传给服务端参与验签） */
export interface CaptchaResult {
  token: string;
  knock?: string;
  dfu?: string;
  ip?: string;
}

declare global {
  interface Window {
    vaptcha?: (options: {
      vid: string;
      container: string;
      lang?: string;
      [key: string]: unknown;
    }) => Promise<VaptchaInstance> | VaptchaInstance;
  }
}

/** 幂等加载 VAPTCHA V4 SDK；已加载则直接 resolve */
function loadScript(): Promise<void> {
  if (window.vaptcha) return Promise.resolve();

  const existing = document.getElementById(SCRIPT_ID) as HTMLScriptElement | null;
  if (existing) {
    if (existing.dataset.loaded === 'true') return Promise.resolve();
    return new Promise((resolve, reject) => {
      existing.addEventListener('load', () => resolve());
      existing.addEventListener('error', () => reject(new Error('VAPTCHA 脚本加载失败')));
    });
  }

  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.id = SCRIPT_ID;
    script.src = SCRIPT_SRC;
    script.async = true;
    script.onload = () => {
      script.dataset.loaded = 'true';
      resolve();
    };
    script.onerror = () => reject(new Error('VAPTCHA 脚本加载失败'));
    document.head.appendChild(script);
  });
}

type CaptchaStatus = 'loading' | 'ready' | 'error';

interface CaptchaChallengeProps {
  /** 验证单元 id（来自 GET /auth/captcha，公开字段） */
  vid: string;
  /** 校验结果变化回调；未通过 / 已重置 / 失败时收到 null */
  onVerify: (result: CaptchaResult | null) => void;
  /** 控件是否不可用（加载中或加载失败），供父表单禁用提交 */
  onUnavailable: (unavailable: boolean) => void;
  /** 递增时清空本次校验结果（例如提交失败后要求重新验证） */
  resetSignal?: number;
}

export function CaptchaChallenge({
  vid,
  onVerify,
  onUnavailable,
  resetSignal = 0,
}: CaptchaChallengeProps) {
  // 作为 container 选择器用的稳定 id；useId 可能含 `:` `«»` 等非法选择器字符，先过滤
  const reactId = useId();
  const containerId = `vaptcha-box-${reactId.replace(/[^a-zA-Z0-9_-]/g, '')}`;

  const instanceRef = useRef<VaptchaInstance | null>(null);
  const [status, setStatus] = useState<CaptchaStatus>('loading');
  const [verifying, setVerifying] = useState(false);
  const [verified, setVerified] = useState(false);

  // 回调放进 ref：避免父组件每次渲染产生的新函数引用触发组件重新初始化
  const onVerifyRef = useRef(onVerify);
  onVerifyRef.current = onVerify;
  const onUnavailableRef = useRef(onUnavailable);
  onUnavailableRef.current = onUnavailable;

  /** 用户点击后主动发起一次校验：resolve 出 token 即回调结果，否则回调 null */
  const runValidate = useCallback(async () => {
    const instance = instanceRef.current;
    if (!instance?.validate) return;
    setVerifying(true);
    try {
      const result = await instance.validate();
      const token = result?.token;
      if (token) {
        // ip 是 token 签名覆盖的字段：本机开发时后端看到的来源 IP 与签名 IP 不同，
        // 必须把它一并回传，服务端才能原样用于验签
        onVerifyRef.current({ token, knock: result?.knock, dfu: result?.dfu, ip: result?.ip });
        setVerified(true);
      } else {
        // 用户主动关闭 / 未完成：明确回到「未通过」，允许重试，但绝不放行
        onVerifyRef.current(null);
        setVerified(false);
      }
    } catch {
      onVerifyRef.current(null);
      setVerified(false);
    } finally {
      setVerifying(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    setVerified(false);
    // 就绪前先禁用提交：绝不能让「还没拿到 token」被当成「校验通过」
    onUnavailableRef.current(true);

    loadScript()
      .then(async () => {
        if (cancelled) return;
        if (!window.vaptcha) {
          throw new Error('VAPTCHA SDK 未就绪');
        }
        const instance = await window.vaptcha({
          vid,
          container: `#${containerId}`,
          lang: 'zh-CN',
        });
        if (cancelled) return;
        instanceRef.current = instance;
        setStatus('ready');
        // 已就绪：是否放行由表单结合「是否拿到 token」判断
        onUnavailableRef.current(false);
      })
      .catch(() => {
        if (cancelled) return;
        setStatus('error');
        onVerifyRef.current(null);
        // 加载失败：保持禁用，绝不静默放行
        onUnavailableRef.current(true);
      });

    return () => {
      cancelled = true;
      instanceRef.current = null;
    };
  }, [vid, containerId]);

  useEffect(() => {
    if (resetSignal === 0) return;
    // 上一次的结果可能已被服务端消费/过期：清掉本地凭证，让用户重新验证
    instanceRef.current?.reset?.();
    setVerified(false);
    onVerifyRef.current(null);
  }, [resetSignal]);

  const triggerDisabled = status !== 'ready' || verifying;

  return (
    <div className={styles.field}>
      <span className={styles.label}>人机校验</span>
      {/* V4 的 container 仅作挂载锚点，SDK 不向其注入可见内容，因此不占高度 */}
      <div id={containerId} className={styles.captchaBox} aria-live="polite" />
      <div className={styles.captchaRow}>
        <button
          type="button"
          className="mini-btn"
          onClick={() => void runValidate()}
          disabled={triggerDisabled}
        >
          {verifying ? '验证中…' : verified ? '重新验证' : '点击完成人机验证'}
        </button>
        {verified && (
          <span className={styles.hint} role="status">
            已完成人机验证
          </span>
        )}
        {status === 'loading' && <span className={styles.hint}>正在加载人机校验…</span>}
        {status === 'ready' && !verified && !verifying && (
          <span className={styles.hint}>请点击按钮完成验证。</span>
        )}
        {status === 'error' && (
          <span className={styles.fieldError} role="alert">
            人机校验加载失败，请刷新页面后重试。
          </span>
        )}
      </div>
    </div>
  );
}
