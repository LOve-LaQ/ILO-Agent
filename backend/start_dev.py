# Quick Start Script for ILO-Agent Demo
"""
一键启动脚本 - 本地开发模式

使用方式:
    python backend/start_dev.py
"""

import subprocess
import sys
import webbrowser
import platform
import socket
import threading
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_ROOT.parent
FRONTEND_URL = "http://127.0.0.1:5173"


def clear_screen():
    """清除终端屏幕"""
    os = platform.system()
    if os == "Windows":
        subprocess.call("cls", shell=True)
    else:
        subprocess.call("clear", shell=True)


def print_header():
    """打印标题"""
    clear_screen()
    print("=" * 60)
    print("  ILO-Agent Demo - Quick Start")
    print("=" * 60)
    print()


def check_redis():
    """检查 Redis 是否运行"""
    try:
        import redis
        r = redis.Redis.from_url("redis://localhost:6379/0", socket_connect_timeout=2)
        r.ping()
        print("✅ Redis running")
        return True
    except Exception as e:
        print(f"❌ Redis not available at localhost:6379")
        return False


def check_qdrant():
    """检查 Qdrant 是否运行

    两个坑：
    1) 必须用 httpx（项目已依赖）而非 requests —— 后者不在 requirements.txt 里，
       import 失败会被 except 吞掉，把「没装库」误报成「Qdrant 没起」。
    2) 必须用 127.0.0.1 而非 localhost —— Windows 会把 localhost 优先解析成 IPv6 ::1，
       而容器里的 Qdrant 只监听 IPv4，直连会拿到 WinError 10054（连接被重置）。
    """
    try:
        import httpx
    except ImportError as e:
        print(f"❌ Qdrant check skipped, httpx not installed: {e}")
        return False

    # 新版 Qdrant 用 /healthz（/status 已 404），为主即可就绪
    for path in ("/healthz", "/status"):
        try:
            resp = httpx.get(f"http://127.0.0.1:6333{path}", timeout=2)
        except Exception as e:
            print(f"❌ Qdrant not available at 127.0.0.1:6333: {e}")
            return False
        if resp.status_code == 200:
            print("✅ Qdrant running")
            return True
    print("❌ Qdrant responded, but no healthy endpoint")
    return False


def check_postgres():
    """检查 PostgreSQL 是否可连（阶段 2 起注册/登录依赖它）"""
    from src.core.db import is_database_configured

    if not is_database_configured():
        print("❌ PostgreSQL not configured (backend/.env 缺 DATABASE_URL)")
        return False
    try:
        from sqlalchemy import text

        from src.core.db import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        print("✅ PostgreSQL running")
        return True
    except Exception as e:
        print(f"❌ PostgreSQL not available: {e}")
        return False


def start_frontend():
    """启动前端开发服务器（Vite）

    只负责拉起进程：浏览器由 start_api() 延迟打开，避免「页面先于 API 打开」。
    """
    frontend_dir = PROJECT_ROOT / "frontend"

    if not frontend_dir.exists():
        print(f"⚠️  Frontend directory not found: {frontend_dir}")
        return None

    if not (frontend_dir / "node_modules").exists():
        print("⚠️  frontend/node_modules 不存在，请先执行：cd frontend && npm install")
        return None

    # 本机 PowerShell 禁止脚本执行（ExecutionPolicy），npm.ps1 会被拦截，
    # 因此 Windows 下显式调用 npm.cmd，而非 npm / npm.ps1
    npm_cmd = "npm.cmd" if platform.system() == "Windows" else "npm"

    print(f"🌐 Starting frontend dev server → {FRONTEND_URL}")
    return subprocess.Popen([npm_cmd, "run", "dev"], cwd=str(frontend_dir))


def resolve_python():
    """优先用 backend 内置虚拟环境解释器，保证依赖齐全"""
    for candidate in (
        BACKEND_ROOT / "ilo" / "Scripts" / "python.exe",  # Windows venv
        BACKEND_ROOT / "ilo" / "bin" / "python",  # POSIX venv
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def wait_for_port(port: int, timeout: float = 60.0) -> bool:
    """轮询 TCP 端口直到可连接（httpx 不可用时的兜底）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def wait_for_api(port: int = 8000, timeout: float = 90.0) -> bool:
    """等 API 真的能响应请求（HTTP 探活，不是 TCP 探活）

    只探 TCP 是不够的：uvicorn 的 reloader 父进程会立刻占住监听套接字，
    端口 1 秒内就「可连接」，而应用还在初始化 LLM 客户端（约 15s），
    这时候打开页面只会看到满屏 502（vite 代理连不上上游）。
    """
    try:
        import httpx
    except ImportError:
        return wait_for_port(port, timeout)

    url = f"http://127.0.0.1:{port}/openapi.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def open_browser_when_ready(api_port: int = 8000, timeout: float = 90.0) -> None:
    """等 API 真正就绪后再打开浏览器"""

    def worker() -> None:
        if wait_for_api(api_port, timeout):
            webbrowser.open(FRONTEND_URL)
        else:
            print(f"⚠️  API 在 {timeout:.0f}s 内未就绪，浏览器未自动打开")

    threading.Thread(target=worker, daemon=True).start()


def main():
    """主函数：检查依赖服务 → 拉起前端 → 前台运行 API"""
    print_header()

    # 步骤 1: 检查依赖服务
    print("Step 1: Checking services...\n")

    redis_ok = check_redis()
    qdrant_ok = check_qdrant()
    postgres_ok = check_postgres()

    if not redis_ok or not qdrant_ok:
        print("\n⚠️  Services are not running. You can use Docker:")
        print("   docker-compose up -d\n")

    if not postgres_ok:
        print("\n⚠️  PostgreSQL 不可用，注册 / 登录会失败。请检查：")
        print("   1) backend/.env 的 DATABASE_URL")
        print("   2) 建表：cd backend && alembic upgrade head\n")

    start_api()


def start_api():
    """启动 API 服务器（前台运行，CTRL+C 停止）"""
    print("\nStep 2: Starting API server...")
    print("   → http://localhost:8000/docs")
    print("   (Press CTRL+C to stop)\n")

    # 先把前端拉起来（vite 不阻塞），再在当前进程前台跑 API：
    # CTRL+C 能直接停掉 API，也不会出现「API 还没就绪就开浏览器」的竞态
    start_frontend()
    open_browser_when_ready()

    print("Step 3: Opening frontend (等 API 就绪后自动打开浏览器)...\n")

    try:
        # 必须把 cwd 切到 backend：uvicorn 的 import 路径以 cwd 为基准，
        # 从项目根运行本脚本时，cwd 里没有 src 包，src.api.main:app 会导入失败
        subprocess.run(
            [
                resolve_python(),
                "-m",
                "uvicorn",
                "src.api.main:app",
                "--reload",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            cwd=str(BACKEND_ROOT),
        )
    except KeyboardInterrupt:
        print("\nServer stopped by user")


if __name__ == "__main__":
    main()
