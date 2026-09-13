# ILO-Agent Demo - 快速开始指南

## ⚡ 5 分钟快速启动（无需复杂配置）

### **步骤 1: 确认环境**

```bash
# 必需安装
Python 3.11+
Docker (可选，仅用于 Redis/Qdrant)

# 验证
python --version  # 应显示 Python 3.11.x
docker --version  # 可选
```

---

### **步骤 2: 克隆项目**

```bash
cd c:\Users\ZY\Documents\ck1\ilo-agent-demo
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
OPENAI_API_KEY=sk-your-openai-api-key-here  # 可选

# 其他配置保持默认即可:
REDIS_URL=redis://localhost:6379/0
QDRANT_URL=http://localhost:6333
```

---

### **步骤 6: 启动 FastAPI Backend**

```bash
# 回到项目根目录
cd ..

# Windows PowerShell
uvicorn ilo-agent-demo.backend.src.api.main:app --reload --host 0.0.0.0 --port 8000

# 或使用 start_dev.py 脚本
python backend/start_dev.py
```

**访问地址:**
- API Docs: http://localhost:8000/docs
- Swagger UI: http://localhost:8000/redoc

---

### **步骤 7: 启动 Frontend**

```bash
# 方式 1: 直接打开 HTML（最简单）
start frontend/index.html

# 方式 2: HTTP 服务器
cd frontend
python -m http.server 3000
# 然后访问：http://localhost:3000
```

---

## 🧪 测试运行

### **方式 A: API 测试**

```bash
# 测试 Discovery Engine
curl http://localhost:8000/api/v1/discover/news

# 测试 Learning Session
curl -X POST http://localhost:8000/api/v1/learning/session \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user",
    "news_item_id": "news-001",
    "time_budget": 15,
    "preferred_depth": "medium"
  }'
```

### **方式 B: 单元测试**

```bash
cd backend
pytest tests/ -v

# 预期输出
✅ TestCreateSession: PASSED
✅ TestProcessQuiz: PASSED
🎉 All tests passed!
```

---

## 📁 项目结构速览

```
ilo-agent-demo/
├── backend/                     # Python 后端
│   ├── src/
│   │   ├── core/               # 配置管理
│   │   ├── api/                # FastAPI 路由
│   │   └── modules/            # 核心业务模块
│   │       └── agent/          # ⭐⭐⭐ LangGraph 状态机 + Mem0
│   ├── tests/                  # 单元测试
│   └── requirements.txt        # Python 依赖
│
├── frontend/                    # HTML5 前端
│   ├── index.html              # 单页面应用
│   └── README.md               # 前端说明
│
├── docker-compose.yml           # Docker 编排
├── demo_script.md              # 📋 面试演示脚本
└── README.md                   # 项目总览
```

---

## 🎯 核心能力清单

这个 Demo 展示了哪些技术能力？

| 模块 | 技术点 | 面试价值 ⭐⭐⭐⭐⭐ |
|------|--------|-----------------|
| **LangGraph 状态机** | 多轮对话流程编排 | ⭐⭐⭐⭐⭐ |
| **Memory Manager** | Redis + Qdrant 分层存储 | ⭐⭐⭐⭐⭐ |
| **FSRS Calculator** | 间隔重复算法实现 | ⭐⭐⭐⭐ |
| **Discovery Engine** | 异步爬虫 + ETL 管道 | ⭐⭐⭐⭐ |
| **FastAPI** | 高性能 API 设计 | ⭐⭐⭐⭐ |
| **Alpine.js 前端** | 轻量级交互 | ⭐⭐⭐ |

---

## ❓ 常见问题解答

### **Q: 不需要 OpenAI API Key 也能运行吗？**
**A:** 可以！当前版本是 Mock 实现，所有讲解内容和测验题目都是硬编码的。如果需要真实 LLM，才必须配置 OPENAI_API_KEY。

### **Q: Redis 和 Qdrant 是必须的吗？**
**A:** 推荐配置但不是必须的。如果没配，系统会使用内存替代，数据会保存在运行时。

### **Q: 前端为什么看起来很简单？**
**A:** 这是刻意设计的极简版，为了展示 Alpine.js + Tailwind CSS 的快速开发能力。真实项目中可以用 React/Vue 替换。

### **Q: 如何扩展更多功能？**
**A:** 
1. 接入真实 RSS 源（GitHub/Twitter）→ 修改 `backend/src/modules/discovery/engine.py`
2. 集成真实 LLM → 在 `state_machine.py` 中调用 OpenAI API
3. 完善推荐算法 → 扩展 `router.py` 中的 70/30 Rule

---

## 💡 下一步建议

1. **立即体验**: 打开浏览器访问 http://localhost:8000/docs，尝试几个 API 接口
2. **阅读代码**: 重点看 `backend/src/modules/agent/state_machine.py`（核心亮点！）
3. **准备面试**: 按照 `demo_script.md` 准备 3 分钟演示
4. **开源分享**: 推送到 GitHub，配置自动部署到 Vercel

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

### **端口冲突**
```bash
# 修改端口（在.docker-compose.yml 或 start_dev.py 中）
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

**祝你好运！** 🚀✨

如有问题，欢迎查阅：
- [`README.md`](../README.md) - 完整项目介绍
- [`demo_script.md`](./demo_script.md) - 面试演示脚本
- [`backend/tests/`](../backend/tests/) - 单元测试用例
