# ILO-Agent Demo —— 单端口生产镜像
#
# 两阶段：Node 构建前端 → Python 3.13 跑后端并同源托管 dist。
# 对外只有 8000 一个端口：/ 是站点，/api/v1 是 API，不需要 nginx、不需要第二条隧道。
# PostgreSQL / Redis / Qdrant 由 docker-compose 编排，不进本镜像。

# ---------- Stage 1: 构建前端 ----------
FROM node:22-alpine AS frontend-build

WORKDIR /frontend
# 先只拷依赖清单，锁文件不变时 npm ci 这一层能命中缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ .
# vite 8 要求 Node >= 20.19，镜像用 22 满足
RUN npm run build


# ---------- Stage 2: 后端运行时 ----------
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LOG_LEVEL=INFO \
    # 单端口同源：后端直接托管 Stage 1 的产物
    SERVE_FRONTEND=true \
    FRONTEND_DIST=/app/frontend/dist

# psycopg2-binary 自带 libpq，不需要 libpq-dev；g++ 仅用于极少数没有 wheel 的依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 运行必需的后端代码：应用源码 + Alembic 迁移
COPY backend/src/ ./src/
COPY backend/alembic/ ./alembic/
COPY backend/alembic.ini ./

# 前端构建产物（只拷 dist，不把 node_modules / 源码带进运行镜像）
COPY --from=frontend-build /frontend/dist/ ./frontend/dist/

# 启动脚本：等库就绪 → 跑迁移 → exec uvicorn
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
