# 运行演示截图清单

本目录存放 README 引用的截图。按文件名放入即可显示，**无需修改 README**。

| 文件名 | 对应环节 | 内容 |
|---|---|---|
| `00-swagger.png` | API 总览 | `/docs` 接口总览（discover / auth / me / admin / learning 五组） |
| `01-feed.png` | 推荐流 | 首页卡片流：标题、中文摘要、标签、星数 |
| `02-refresh.png` | 抓取任务 | 点「抓取最新」后的阶段进度提示 |
| `03-session.png` | 学习会话 | 会话号与驱动模型，刷新后由服务端恢复 |
| `04-chat.png` | AI 解读 | 多轮追问；「资料里没有」体现检索边界 |
| `05-quiz.png` | 测验评分 | Swagger 调用 `/learning/quiz` 的响应（逐题判定 + FSRS 间隔） |
| `06-fsrs.png` | FSRS 复习 | `/learning/complete` 的评级与下次复习日期 |
| `07-flow.png` | 全链路 | 后端日志：初始化 → 建会话 → 讲解 → 评分 → 归档 |
| `08-ranking.png` | GitHub 新星榜 | 本周 · 全部语言，按日均涨星排序 |
| `09-search.png` | 仓库搜索 | `vector database` 的筛选与结果列表 |

## 拍摄约定

- 桌面端统一 1280 宽、隐藏滚动条；移动端用 CDP `Emulation.setDeviceMetricsOverride` 设 390×844
  （`--window-size` 不等于真实视口，直接用它截移动端会误判成横向溢出）
- 终端截图用深色主题、放大字号，确保 `score` / `rating` / `new_interval` 可读
- 隐藏书签栏与个人信息；截图环境用 `CAPTCHA_PROVIDER=off RATE_LIMIT_ENABLED=false`，拍完改回
