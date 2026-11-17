# 前端分包对比记录

> 目的：把「为什么这么切、切完效果如何、踩过什么坑」固化下来。
> 数据由 `npm run bundle:report -- --save <name>` 生成，快照在本目录：
> `baseline.json`（分包前）/ `after-split.json`（分包后）。任何人可重跑复现。

## 一句话结论

单个 775 KB 的巨型 chunk 被切成 5 块，**最大块从 775 KB 降到 346 KB**，消除了 Vite 的 500 KB 警告；
**总 gzip 体积几乎不变**（242.7 → 243.7 KB）——分包的收益是**缓存复用与并行下载**，
不是字节减少。要真正减小首屏下载量，需要按需加载（见文末下一步）。

## 数据对比

| 指标 | 分包前 | 分包后 | 说明 |
|---|---|---|---|
| 最大 JS chunk | 774.7 KB / gzip 230.9 KB | 345.7 KB / gzip 99.3 KB | markdown 链，仍是最大块但已低于警告线 |
| chunk 数量 | 1 | 5（+1 个 0.7 KB 运行时桥接） | |
| JS+gzip 总量 | 242.7 KB | 243.7 KB | +1 KB 来自多出来的运行时桥接与 chunk 边界开销，可忽略 |
| 500 KB 警告 | 有 | 无 | `npm run build` 已确认 |
| 首屏 modulepreload | 全部 | 全部（5 块） | **关键**：首屏下载量没变，见下 |

分包后各块（gzip）：

| chunk | 大小 | 内容 | 变更频率 |
|---|---|---|---|
| `markdown` | 99.3 KB | react-markdown + unified/remark/rehype/micromark 全家桶 | 几乎不变 |
| `vendor` | 56.3 KB | react / react-dom / scheduler | 几乎不变 |
| `index`（entry） | 41.3 KB | 业务代码（features / shared / UI） | 每次发版都变 |
| `router` | 32.0 KB | react-router / @remix-run | 几乎不变 |
| `state` | 10.4 KB | @tanstack/react-query / zustand | 几乎不变 |

**缓存收益的具体含义**：业务代码改一行，用户只需重新下载 41 KB 的 entry；
99 + 56 + 32 + 10 = 197 KB 的稳定依赖全部命中浏览器缓存。分包前则是 775 KB 全量重下。

## 为什么按「依赖边界」而不是按体积切

按体积硬切（比如每 200 KB 一块）会让一个依赖库被劈到两个 chunk，产生跨块引用，
既抵消并行收益又容易踩循环依赖。这里按「谁和谁一起变」切：

- 框架核心（react / router / 状态库）由 `package.json` 锁版本，只有升级依赖才变；
- markdown 解析链是一个内聚的子树，且只有学习抽屉和记录详情用得到；
- 业务代码独立成 entry，发版时只有它失效。

配置在 `frontend/vite.config.ts` 的 `manualChunks`。命名前缀（`vendor-`/`router-`/`state-`/`markdown-`）
与 `scripts/bundle-report.mjs` 的 `classify()` 严格对应——两处必须同步，否则报告会用两把尺子。

## 踩过的坑（可回溯的关键）

### 1. 子串匹配误伤，react 被塞进 markdown 块

第一版 `manualChunks` 用正则子串匹配（`react-markdown|...|extend`）。两个问题：

- `extend` 这类通用词会命中别的库；
- 更隐蔽的是 Rolldown 把 `react-markdown` 依赖的 `react` 一起拉进了 markdown 块。
  结果 `router`/`state`/`vendor` 全都反向依赖 markdown——等于没拆干净。

**定位方法**：`npx vite build --sourcemap`，再解析 `dist/assets/*.js.map` 的 `sources`，
直接读出每个 chunk 里到底有哪些 `node_modules` 包。肉眼看 chunk 名是看不出来的。

**修法**：不用子串匹配，改为 `packageName()` 精确取出 `node_modules/` 后的包名
（含 `@scope`），用完整包名判断。`react` 与 `react-markdown` 是两个独立的整名，不再混淆。

### 2. Service Worker 风险排查后排除

拆分 chunk 最怕 SW 缓存了旧 chunk 导致用户「改了代码页面不变」。
核对 `frontend/public/sw.js`：它刻意**不缓存任何静态资源**，只接管导航请求交回网络。
所以分包不会造成缓存错配，无需改 SW。

### 3. 浏览器验证脚本的两个假阳性

SPA 客户端导航命中内存缓存时 `resource.transferSize === 0`，快速切路由会取消上一个请求
报 `net::ERR_ABORTED`——两者都不是加载失败。验证判据只认三类真实故障：
console 异常、根容器无子节点、页面文本为空。

## 验证结果

- `npm run build`：`tsc -b` 通过、467 模块、无体积警告、无 sourcemap 残留
- 浏览器（CDP 驱动 headless Chrome，1280×900）：`/`、`/login`、`/register`、
  `/legal/privacy`、404 路由全部 `root>0`、console 异常 0、真实网络失败 0
- 产物由 FastAPI 同源托管（`SERVE_FRONTEND`），实测 8000 端口已返回新 chunk 文件名

## 下一步：真正减小首屏（未做）

当前 Vite 给 entry 的每个前置依赖都注入了 `modulepreload`，5 块在首屏全部预取，
所以首屏总下载量仍是 ~244 KB gzip。要再降，需要让 markdown 块（96 KB）按需加载：

1. `MarkdownBody` 改为 `React.lazy(() => import(...))`；
2. 首页 `DiscoveryPage` 无条件挂载的 `LearningDrawer` 改为条件挂载
   （用户点「开始讲解」时才挂），否则 lazy 也会被首屏触发；
3. 「我的 → 学习记录详情」的 `SessionDetail` 同样处理。

这一步改动面较大（涉及抽屉挂载时机与恢复逻辑），且 qdrant 当前不可用无法完整端到端验证
学习链路，所以留到后续单独做。分包本身已是可独立交付、可回溯的一版。
