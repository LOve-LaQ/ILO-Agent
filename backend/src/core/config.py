# Core Configuration Module
"""
ILO-Agent Demo 核心配置
- 环境变量管理
- 日志系统初始化
"""

import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings
from loguru import logger

from src.core.context import get_request_id

# 全部路径锚定到 backend 目录，而非当前工作目录：
# 否则 `python backend/start_dev.py` / `python backend/src/api/main.py` 从项目根运行时，
# .env 会读不到（DATABASE_URL / JWT_SECRET 全空，认证静默失效），logs 也会落到项目根。
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_ROOT / ".env"
LOG_DIR = BACKEND_ROOT / "logs"


class Settings(BaseSettings):
    """应用配置项"""
    
    # API Keys
    # Optional - use DEEPSEEK_API_KEY in state_machine instead
    openai_api_key: str = ""
    redis_url: str = "redis://127.0.0.1:6379/0"
    qdrant_url: str = "http://127.0.0.1:6333"
    mem0_api_key: str = ""

    # GitHub（「学习先读原文」要抓仓库 README）
    # 匿名调用 GitHub API 只有 60 次/小时，预取 README 会迅速耗尽；配 token 后可到 5000/h。
    # 留空则自动降级到 raw CDN（不占 API 配额，但拿不到 blob sha）。
    github_token: str = ""
    
    # Application
    log_level: str = "INFO"
    environment: str = "development"

    # CORS 允许的来源（逗号分隔的白名单）。
    # 【为什么不给 "*"】allow_origins=["*"] 与 allow_credentials=True 同时开启是明确的
    # 纵深防御破口：浏览器规范虽然不会让该组合真正放行，但配置本身已失去意义。
    # 留空时回退到 frontend_base_url（开发期 vite proxy 走同源，本不需要跨域）。
    cors_allow_origins: str = ""

    # Database（阶段 2 用户体系 / 阶段 3 可追溯链路）
    # 形如 postgresql+psycopg2://<user>:<pwd>@127.0.0.1:5432/ilo_agent
    # 故意不给默认值：未配置时 core/db.py 会抛出可操作的错误，而不是静默连错库。
    database_url: str = ""
    db_echo: bool = False

    # Auth / JWT（阶段 2 权限边界）
    # jwt_secret 必须 >= 32 字节（PyJWT 对 HS256 有最短长度告警）
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14
    refresh_cookie_name: str = "ilo_refresh"
    cookie_secure: bool = False
    cookie_domain: str = ""

    # 入口加固（阶段 5）：限流与登录失败锁定
    # 限额配 0 或负数表示「该规则不生效」，便于排查时临时放行
    rate_limit_enabled: bool = True
    register_rate_limit_per_hour: int = 5
    login_rate_limit_ip_per_15min: int = 20
    login_rate_limit_account_per_15min: int = 10
    refresh_rate_limit_per_hour: int = 60
    forgot_rate_limit_per_hour: int = 5
    forgot_email_rate_limit_per_hour: int = 3
    captcha_rate_limit_per_hour: int = 30
    # 确认邮件重发：注册后提示「邮箱未验证」时用户容易反复点重发，
    # 需要压住，否则等于给 SMTP 配额开了一个自助消耗口
    email_resend_rate_limit_per_hour: int = 3
    # 手动抓取（/discover/refresh*）：每次都会打外部 API + 付 LLM 摘要成本，
    # 且匿名可触发 —— 必须限流，否则等于给成本开了一个公开的放大口
    discover_refresh_rate_limit_per_hour: int = 20
    # 原文按需补抓（/discover/cards/{id}/content）：匿名可触发一次外部抓取并回写
    card_content_rate_limit_per_hour: int = 60
    # 中文导读（/discover/cards/{id}/digest）：每次未命中缓存都要真实调用 LLM 读
    # README 并生成导读，是明确的付费放大面 —— 配额比原文接口更紧
    card_digest_rate_limit_per_hour: int = 30
    # 重置密码：独立的 IP 维度配额，不与 captcha 校验共用一个桶（否则互相抢占）
    password_reset_rate_limit_per_hour: int = 10
    # 学习问答（/learning/chat）：每轮都真实调用 LLM，是成本最高的常规接口
    learning_chat_rate_limit_per_hour: int = 60
    # 首屏资讯（/discover/news）：本身是廉价读，但登录用户会走一次向量检索 +
    # 全量候选排序；不给额度就留下一个可脚本化放大的读口（每刷一次都扫全库）
    news_rate_limit_per_hour: int = 600
    # 撤销注销（/auth/account/delete/cancel）：未登录可调且要校验密码，
    # 不设防就会变成绕开登录锁定的口令爆破旁路
    account_cancel_rate_limit_per_hour: int = 10
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # 人机校验（阶段 5）：VAPTCHA V4
    # provider 配 off 只允许在非生产环境，生产用 off 会在启动时直接报错
    captcha_provider: str = "vaptcha"
    vaptcha_vid: str = ""
    # 控制台里的 VKEY（保密，仅服务端使用；提交给 official 校验接口时字段名为 vkey）
    vaptcha_key: str = ""
    vaptcha_verify_url: str = "https://v41.vaptcha.com/api/verify"
    # 旧字段：VAPTCHA V4 已不再返回 score，当前不参与判定，仅为兼容旧配置保留
    vaptcha_min_score: int = 0

    # 会话（阶段 5）：refresh 轮换遇到「同一旧 token 被并发使用」时的宽限窗口。
    # 多标签页共享同一个 refresh Cookie，几乎同时刷新时会各带旧 token 发一次请求，
    # 没有宽限窗就会把正常并发误判成 token 盗用并踢掉所有会话。
    session_rotate_grace_seconds: int = 30

    # 邮件发信（阶段 5）
    # console 把邮件写进日志并落 email_outbox，仅限开发环境
    email_provider: str = "console"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    frontend_base_url: str = "http://127.0.0.1:5173"
    password_reset_expire_minutes: int = 30
    # 邮箱确认链接有效期。做得比重置密码长得多：确认邮箱不是安全敏感操作，
    # 只是让用户有机会发现「邮箱填错了」，有效期长些才不会一直提示未验证。
    email_verification_expire_hours: int = 72

    # 账号注销（阶段 5）：冷静期内可撤销，到期执行匿名化
    account_deletion_grace_days: int = 15

    # 个人信息保护（阶段 5）
    # IP 落库前用该 secret 做 HMAC；留空则**不记录** IP（宁可不记，也不落明文）
    ip_hash_secret: str = ""
    activity_retention_days: int = 180
    session_retention_days: int = 90
    email_outbox_retention_days: int = 30
    # 过期的密码重置令牌保留 7 天（留一点窗口供排查，之后清掉）
    password_reset_retention_days: int = 7
    # 隐私政策 / 用户协议的版本号；写入 consent_records，改版后据此判断是否需要重新获取同意
    legal_document_version: str = "2026-01"

    # 兴趣向量个性化推送
    # enabled 是总开关：关掉后 /news 直接走原来的随机抽样，不读画像也不做排序
    interest_profile_enabled: bool = True
    # 行为时间衰减半衰期（天）：越久远的行为对画像贡献越小。
    # 取 14 天——技术兴趣的迁移周期大致是「两周换一批关注点」，再长会把旧兴趣拖进来
    interest_profile_half_life_days: int = 14
    # 参与构建画像的候选卡片上限（按信号权重取 top-N）
    # 既是成本闸门（每张要取向量），也避免「早期一次性浏览」长期主导画像
    interest_profile_max_candidates: int = 200

    # Agent Configuration
    max_tokens: int = 4096
    num_questions: int = 3
    
    # Rate Limiting
    request_rate_limit: int = 100  # requests per minute
    
    class Config:
        env_file = str(ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "ignore"


# 全局配置实例
settings = Settings()


def _patch_record(record: dict) -> None:
    """给每条日志补上 request_id（阶段 3 请求链路可观测）"""
    record["extra"].setdefault("request_id", get_request_id() or "-")


def setup_logger():
    """初始化日志系统"""
    logger.remove()  # 移除默认处理器

    # patcher 是 logger.configure() 的参数，不是 logger.add() 的 ——
    # 传给 add() 会被当成文件 sink 的参数透传给 open()，启动即 TypeError。
    logger.configure(patcher=_patch_record)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_format = (
        "{time:YYYY-MM-DD HH:mm:ss} | {level} | rid={extra[request_id]} | "
        "{name}:{function}:{line} - {message}"
    )

    # 默认 handler 已被 remove，这里显式补回控制台输出：
    # 否则应用日志只落文件，本地开发时控制台是全瞎的。
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=log_format,
        colorize=True,
    )

    logger.add(
        LOG_DIR / "app_{time:YYYY-MM-DD_HH-mm-SS}.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.log_level,
        format=log_format,
        encoding="utf-8",  # 日志里有 emoji，Windows 默认 GBK 会编码失败
    )
    
    logger.info(f"🚀 ILO-Agent Demo started in {settings.environment} mode")


__all__ = ["settings", "setup_logger"]
