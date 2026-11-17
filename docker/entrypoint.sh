#!/bin/sh
# 容器启动顺序：等 PostgreSQL 就绪 → 跑迁移 → 把 PID 交还给 uvicorn。
#
# 【为什么要等】compose 的 depends_on: service_healthy 只保证 PG 进程在监听，
# 不保证已经接受连接；迁移先于建表完成会直接 connection refused 让容器退出。
# 【为什么要 exec】不 exec 的话 shell 是 PID 1，收不到 SIGTERM，docker stop 会等满 10s。
set -e

python - <<'PY'
import os
import sys
import time

from sqlalchemy import create_engine, text

url = (os.environ.get("DATABASE_URL") or "").strip()
if not url:
    sys.exit("DATABASE_URL 未配置，无法启动")

engine = create_engine(url, pool_pre_ping=True)
for attempt in range(1, 31):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"[entrypoint] PostgreSQL 就绪（第 {attempt} 次探测）", flush=True)
        break
    except Exception as exc:  # noqa: BLE001 - 启动期等库是预期行为
        if attempt == 30:
            sys.exit(f"PostgreSQL 30 次探测均失败，最后错误：{exc}")
        time.sleep(1)
PY

echo "[entrypoint] 执行 Alembic 迁移..."
alembic upgrade head

echo "[entrypoint] 启动应用：$*"
exec "$@"
