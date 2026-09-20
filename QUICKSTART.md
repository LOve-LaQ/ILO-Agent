# ILO-Agent Demo - 快速开始指南

## ⚡ 5 分钟快速启动（无需复杂配置）

### **步骤 1: 确认环境**

```bash
# 必需安装
Python 3.11+                  # 本项目在 Python 3.13 上开发验证
PostgreSQL 14+                # 阶段 2 起必需：用户注册 / 登录
Node.js 18+                   # 前端 React 工程
Docker (可选，仅用于 Redis/Qdrant)

# 验证
python --version
node --version
docker --version  # 可选
```

---

### **步骤 2: 克隆项目**

```bash
git clone https://github.com/LOve-LaQ/ILO-Agent.git
cd ILO-Agent
```

---

### **步骤 3: 启动依赖服务（Redis + Qdrant）**

#### **选项 A: 使用 Docker（推荐）**

```bash
# 一键启动所有服务
docker-compose up -d

# 查看状态
docker-compose ps

# 预期输出
redis         # ✅ running
qdrant        # ✅ running
```

#### **选项 B: 本地安装（备选）**

- **Redis**: https://github.com/microsoftarchive/redis/releases
- **Qdrant**: `pip install qdrant-client` (只需 client，不需服务端)

#### **选项 C: PostgreSQL（阶段 2 起必需）**

用户注册 / 登录依赖 PostgreSQL；建表由 Alembic 负责。

```bash
# 1) 建应用角色与库（本机已装 PostgreSQL 时；会提示输入 postgres 超级用户密码）
#    Windows 路径按实际安装版本调整
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h 127.0.0.1 `
  -c "CREATE ROLE ilo LOGIN PASSWORD 'ilo_dev_2026';" `
  -c "CREATE DATABASE ilo_agent OWNER ilo;"

# 2) 建表（连接串的唯一来源是 backend/.env 的 DATABASE_URL）
cd backend
alembic upgrade head
```

---

### **步骤 4: 安装 Python 依赖**

```bash
cd backend
pip install -r requirements.txt

# 或指定国内镜像加速
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

### **步骤 5: 配置环境变量**

```bash
# 复制示例文件
copy .env.example .env

# 编辑.env，至少填写以下内容:

# 1) LLM（可选；不填会降级到内置示例内容）
DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here

# 2) PostgreSQL（阶段 2 起必需）
DATABASE_URL=postgresql+psycopg2://ilo:ilo_dev_2026@127.0.0.1:5432/ilo_agent

# 3) JWT 密钥（必填，至少 32 字节；生成方式见下）
#    python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET=

# 其他配置保持默认即可:
REDIS_URL=redis://localhost:6379/0
QDRANT_URL=http://localhost:6333
```

---

### **步骤 6: 启动 FastAPI Backend**

```bash
# 方式 1: 从项目根目录运行一键启动脚本
python backend/start_dev.py

# 方式 2: 手动启动（需先进入 backend 目录）
cd backend
python src/api/main.py
```

**访问地址:**
- API Docs: http://localhost:8000/docs
- Swagger UI: http://localhost:8000/redoc

> 首次启动后端需要 10~20 秒初始化 LLM 客户端，日志出现 `Application startup complete` 才算真正就绪。
> 这段时间里接口全是 502 / 连接被拒 —— `start_dev.py` 会等 API 通过 HTTP 探活后再打开浏览器，
> 手动启动时请等到就绪提示再访问。

---

### **步骤 7: 启动 Frontend**

```bash
cd frontend
npm install
npm run dev
# 然后访问：http://127.0.0.1:5173
# （vite 已配 /api 反向代理到 127.0.0.1:8000，前后端同源，无需处理跨域）
```

> 改了后端契约后请执行 `npm run gen:api` 重新生成前端类型（`src/shared/api/schema.d.ts`），
> 前端接口类型一律由 OpenAPI 派生，禁止手写。

---

## 🧪 测试运行

### **API 测试**

```bash
# 匿名可读
curl http://localhost:8000/api/v1/discover/news

# 注册（返回 access token，refresh token 走 httpOnly Cookie）
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","username":"you","password":"Str0ng-Passw0rd"}'

# 登录
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"identifier":"you@example.com","password":"Str0ng-Passw0rd"}'

# 学习会话必须带 access token；
# user_id 由后端从 token 的 sub 解析，请求体不再接受 user_id
curl -X POST http://localhost:8000/api/v1/learning/session \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <access_token>" \
  -d '{"news_item_id":"news-001","time_budget":15,"preferred_depth":"medium"}'
```

---

## 📁 项目结构速览

```
ilo-agent-demo/
├── backend/                     # Python 后端
│   ├── src/
│   │   ├── core/               # 配置 / 数据库引擎 / 密码与 JWT
│   │   ├── models/             # SQLAlchemy ORM（users）
│   │   ├── schemas/            # Pydantic 契约（唯一契约源）
│   │   ├── api/                # FastAPI 路由（discover / auth / learning）
│   │   └── modules/            # 核心业务模块
│   │       └── agent/          # ⭐⭐⭐ 学习状态机 + 分层记忆
│   ├── alembic/                # 数据库迁移
│   ├── tests/                  # pytest（契约 + 权限边界）
│   ├── news_scheduler.py       # 资讯抓取定时调度
│   ├── start_dev.py            # 一键启动脚本
│   └── requirements.txt        # Python 依赖
│
├── frontend/                    # React 19 + TypeScript + Vite
│   └── src/                    # features / shared / components
│
├── docker-compose.yml           # Docker 编排（Redis + Qdrant）
└── README.md                   # 项目总览
```

---

## 🎯 核心能力清单

这个 Demo 展示了哪些技术能力？

| 模块 | 技术点 | 亮点 |
|------|--------|------|
| **学习状态机** | 多轮对话流程编排 | 状态流转清晰、易扩展 |
| **Memory Manager** | Redis + Qdrant 分层存储 | 短期/长期记忆分离 |
| **FSRS Calculator** | 间隔重复算法实现 | 理论算法工程落地 |
| **Discovery Engine** | 异步爬虫 + ETL 管道 | 多源抓取与去重 |
| **FastAPI** | 高性能 API 设计 | 异步接口 + 异常兜底 |
| **React 19 + TypeScript** | 前后端契约派生 | 类型由 OpenAPI 生成，零手写 |
| **认证与权限边界** | JWT + httpOnly Cookie | access 存内存 / refresh 存 Cookie，user_id 只认 token |

---

## ❓ 常见问题解答

### **Q: 不需要 OpenAI API Key 也能运行吗？**
**A:** 可以！未配置 LLM 时系统会降级到内置示例内容，保证整条流程可跑通。配置真实的 LLM API Key（如 DeepSeek）后即可生成真实讲解与测验。

### **Q: Redis 和 Qdrant 是必须的吗？**
**A:** 推荐配置但不是必须的。如果没配，系统会使用内存替代，数据会保存在运行时。

### **Q: 前端为什么看起来很简单？**
**A:** 视觉上刻意保持克制（干净明亮的信息流），但工程结构是完整的：React 19 + TypeScript + Vite，按 feature 切分模块，接口类型全部由后端 OpenAPI 派生，登录态走 JWT + httpOnly Cookie。

### **Q: 如何扩展更多功能？**
**A:** 
1. 接入真实 RSS 源（GitHub/Twitter）→ 修改 `backend/src/modules/discovery/engine.py`
2. 集成真实 LLM → 在 `state_machine.py` 中调用 OpenAI API
3. 完善推荐算法 → 扩展 `memory_manager.py` 中的相似用户协同过滤

---

## 💡 下一步建议

1. **立即体验**: 打开浏览器访问 http://localhost:8000/docs，尝试几个 API 接口
2. **阅读代码**: 重点看 `backend/src/modules/agent/state_machine.py`（核心亮点！）
3. **体验完整流程**: 通过前端页面走通“讲解 → 测验 → 复习规划”完整链路
4. **部署上线**: 推送到 GitHub，可选配置 Docker / Vercel 部署

---

## 🆘 遇到问题？

### **Redis 连接失败**
```bash
# 检查 Redis 是否运行
docker-compose ps redis

# 重启
docker-compose restart redis

# 本地测试
redis-cli ping  # 应该返回 PONG
```

### **PostgreSQL 未配置 / 连不上（阶段 2 起）**
```bash
# 症状：启动报「未配置 DATABASE_URL，无法连接数据库」或注册接口 500
# 1) 确认 backend/.env 里有 DATABASE_URL（格式见 backend/.env.example）
# 2) 确认库和表都在
psql "postgresql://ilo:ilo_dev_2026@127.0.0.1:5432/ilo_agent" -c "\dt"   # 应看到 users / alembic_version
# 3) 缺表就补上
cd backend && alembic upgrade head
```

### **页面能打开但接口全报 502 / 连接被拒**
```bash
# 原因通常是后端还在冷启动（要初始化 LLM 客户端，10~20s）
# 1) 看后端日志是否已出现 Application startup complete
# 2) curl 直接探活后端（绕过 vite 代理）
curl http://localhost:8000/openapi.json
```

### **端口冲突**
```bash
# 修改端口（在 docker-compose.yml 或 start_dev.py 中）
PORT=8001  # 改为其他未被占用的端口
```

### **Python 版本不兼容**
```bash
# 降级 Python 版本
python -m pip install --upgrade pip setuptools wheel

# 重新安装依赖
pip install -r requirements.txt
```

---

祝你使用愉快！ 🚀

相关文档：
- [`README.md`](./README.md) - 完整项目介绍
- API 文档：http://localhost:8000/docs
