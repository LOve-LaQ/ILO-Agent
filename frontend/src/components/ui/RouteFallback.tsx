import { isRouteErrorResponse, Link, useRouteError } from 'react-router-dom';

import styles from './RouteFallback.module.css';

/**
 * 路由兜底页 —— 上线前必须有的那一层。
 *
 * 【为什么不能没有】createBrowserRouter 在未匹配路由或渲染抛错时，会把 React Router
 * 写给开发者的提示直接渲染进页面（"Hey developer 👋 You can provide a way better UX
 * than this..."）。那句话对用户毫无意义，还暴露了技术栈。根路由挂 errorElement 后，
 * 404 与运行时异常都落到这里，外链失效、接口异常导致的白屏都有了体面的出口。
 *
 * 【为什么 404 单独画】通用错误页让用户「刷新重试」是合理的，但 404 刷新一百次
 * 也不会好转 —— 它真正需要的是回到有内容的地方，所以直接给首页与搜索两个入口。
 */
export function NotFoundPage() {
  return (
    <main className={styles.page}>
      <section className={styles.card}>
        <div className={styles.code} aria-hidden="true">
          404
        </div>
        <h1 className={styles.title}>这个页面找不到了</h1>
        <p className={styles.desc}>
          链接可能已经失效，或者地址输错了。回首页看看最近有哪些值得关注的项目。
        </p>
        <div className={styles.actions}>
          <Link to="/" className="btn btn-primary">
            返回首页
          </Link>
          <Link to="/?view=search" className="btn">
            去搜仓库
          </Link>
        </div>
      </section>
    </main>
  );
}

/** 非 404 的路由级错误：不暴露错误对象，只给用户一条可执行的出路 */
function UnexpectedError() {
  return (
    <main className={styles.page}>
      <section className={styles.card}>
        <div className={styles.code} aria-hidden="true">
          !
        </div>
        <h1 className={styles.title}>页面暂时开不出来</h1>
        <p className={styles.desc}>
          可能是网络抖了一下。刷新重试，如果一直这样，回首页继续浏览。
        </p>
        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => window.location.reload()}
          >
            刷新重试
          </button>
          <Link to="/" className="btn">
            返回首页
          </Link>
        </div>
      </section>
    </main>
  );
}

export function RouteError() {
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) {
    return <NotFoundPage />;
  }
  // 错误详情只写控制台给开发者，不往页面上放：真实报错里常带接口地址与内部字段
  if (import.meta.env.DEV) {
    console.error('[route error]', error);
  }
  return <UnexpectedError />;
}
