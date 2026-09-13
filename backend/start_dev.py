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
from pathlib import Path


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
    """检查 Qdrant 是否运行"""
    try:
        import requests
        resp = requests.get("http://localhost:6333/status", timeout=2)
        if resp.status_code == 200:
            print("✅ Qdrant running")
            return True
        return False
    except:
        print(f"❌ Qdrant not available at localhost:6333")
        return False


def start_frontend():
    """打开前端页面"""
    frontend_path = Path(__file__).parent.parent / "frontend" / "index.html"
    
    if frontend_path.exists():
        webbrowser.open(frontend_path.absolute().as_uri())
        print(f"🌐 Frontend opened: {frontend_path}")
    else:
        print(f"⚠️  Frontend file not found: {frontend_path}")


def main():
    """主函数"""
    print_header()
    
    # 步骤 1: 检查依赖服务
    print("Step 1: Checking services...\n")
    
    redis_ok = check_redis()
    qdrant_ok = check_qdrant()
    
    if not redis_ok or not qdrant_ok:
        print("\n⚠️  Services are not running. You can use Docker:")
        print("   docker-compose up -d\n")
    
def start_api():
    """启动 API 服务器"""
    import subprocess
    
    print("\nStep 2: Starting API server...")
    print("   → http://localhost:8000/docs")
    print("   (Press CTRL+C to stop)\n")
    
    try:
        # 直接运行 uvicorn module
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "src.api.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]
        )
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    
    # 步骤 3: 打开前端
    print("\nStep 3: Opening frontend...")
    start_frontend()


if __name__ == "__main__":
    main()
