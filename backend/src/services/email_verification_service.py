# Email Verification - 注册确认邮件
"""阶段 5 补充：注册后发一封「确认这个邮箱收得到信」的邮件。

**为什么注册不阻塞、却仍要发这封信**：注册的入口闸门只用人机校验（见
`schemas/auth.py` 的 `CaptchaFields` 注释），不做邮箱/短信验证码。代价是
「邮箱填错也能注册成功」，而邮箱一错，找回密码这条路就断了 —— 麻烦的是用户
**当下不会知道**，往往等到真的忘了密码才发现。

这封信就是用来提前暴露这个问题的：收不到，就说明邮箱可能填错了。因此它刻意
**不影响任何主流程** —— 不阻塞注册、不限制登录、不确认也能正常用。

**为什么用 JWT 而不是建一张一次性令牌表**：确认邮箱是**幂等**状态（点一次和点
十次结果一样），不像重置密码那样「用过必须作废、必须防重放」。所以不需要落库的
`used_at`，用一枚 `typ=email_verify` 的载荷 JWT 就够了（见 `core/security.create_token`）。
好处是：不加新表、不加迁移、也不用进保留期清理。代价是无法主动作废 —— 而对一个
「只是把邮箱标记为可达」的动作来说，这个代价可以接受。
"""

from typing import Optional

import jwt

from src.core.config import settings
from src.core.security import create_token, decode_token
from src.models.user import User
from src.services.email_service import send_email

# 与 access / refresh 并列的 token 类型。靠 `typ` 保证：确认链接不能被当成
# 登录凭证使用，反过来登录 token 也过不了确认校验。
EMAIL_VERIFY_TYPE = "email_verify"


def issue_token(user: User) -> str:
    """签发确认令牌（放进邮件链接）"""
    token, _ = create_token(str(user.id), EMAIL_VERIFY_TYPE)
    return token


def resolve_token(token: Optional[str]) -> Optional[str]:
    """核验确认令牌，有效则返回 user_id，否则返回 None。

    不区分「签名不对」「类型不对」「已过期」—— 对调用方而言都是「链接不可用」，
    合并成一个 None 可以避免把失败原因泄露成可枚举的信号。
    """
    if not token:
        return None
    try:
        payload = decode_token(token, EMAIL_VERIFY_TYPE)
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) and subject else None


def _body_text(username: str, link: str) -> str:
    return (
        f"你好 {username}：\n\n"
        "感谢注册 ILO-Agent。请点击下面的链接，确认这个邮箱能收到我们的邮件：\n\n"
        f"{link}\n\n"
        f"链接 {settings.email_verification_expire_hours} 小时内有效。\n"
        "这不是登录链接，你的账号现在就能正常使用，确认只是为了将来能顺利找回密码。\n\n"
        "如果这不是你本人的操作，忽略本邮件即可，账号不会受到任何影响。\n"
    )


def send_verification_email(user: User) -> None:
    """发出确认邮件。

    best-effort：`send_email` 内部会吞掉发信异常（见 services/email_service），
    所以这里不必再兜一层 —— 注册不能因为发信失败而失败。
    """
    link = (
        f"{settings.frontend_base_url.rstrip('/')}/verify-email"
        f"?token={issue_token(user)}"
    )
    send_email(
        user.email,
        "确认你的 ILO 邮箱",
        _body_text(user.username, link),
        purpose="email_confirm",
    )


__all__ = [
    "EMAIL_VERIFY_TYPE",
    "issue_token",
    "resolve_token",
    "send_verification_email",
]
