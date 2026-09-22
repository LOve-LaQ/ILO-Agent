# Auth Schemas - 认证契约
"""阶段 2 权限边界 / 阶段 5 入口加固的接口契约。

- access token 走响应体（前端存内存），refresh token 只走 httpOnly Cookie，
  前端 JS 读不到，降低 XSS 下的凭证泄露面
- 登录标识用 identifier 同时接受邮箱与用户名
- 阶段 5：注册/登录/找回/重置/改密/注销/会话管理的请求与响应模型，
  用户名规范（core/username_policy）与密码策略（core/password_policy）在这里落地
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from src.core.password_policy import check_password
from src.core.security import MAX_PASSWORD_BYTES
from src.core.username_policy import check_username


class CaptchaFields(BaseModel):
    """人机校验请求字段（VAPTCHA V4）。

    `captcha_token` 是凭证；`captcha_knock` / `captcha_dfu` / `captcha_ip` 是被 token 签名
    覆盖的伴随数据，由前端 `validate()` 一并返回 —— 服务端校验时必须原样回传，否则上游验签失败。
    """

    captcha_token: Optional[str] = Field(
        None, description="人机校验 token（服务端强制校验，缺失即拒绝）"
    )
    captcha_knock: Optional[str] = Field(
        None, description="VAPTCHA knock（前端 validate() 返回，须原样回传）"
    )
    captcha_dfu: Optional[str] = Field(
        None, description="VAPTCHA dfu（前端 validate() 返回，须原样回传）"
    )
    captcha_ip: Optional[str] = Field(
        None,
        description="VAPTCHA 签名所用客户端 IP（前端 validate() 返回，服务端优先采用它验签）",
    )


class RegisterRequest(CaptchaFields):
    """POST /auth/register 请求体"""

    email: EmailStr
    username: str = Field(..., min_length=2, max_length=50, description="用户名")
    password: str = Field(
        ...,
        min_length=8,
        max_length=MAX_PASSWORD_BYTES,
        description=f"密码（{8}-{MAX_PASSWORD_BYTES} 字节）",
    )
    accept_terms: bool = Field(
        False, description="是否已同意用户协议与隐私政策（前端强制勾选）"
    )

    @model_validator(mode="after")
    def _validate_credentials(self) -> "RegisterRequest":
        # 用户名与密码策略是「规则」而不是「类型」，放在 model_validator 里才能
        # 拿到 email/username 一起做「与身份信息重合」的判定。
        username_reason = check_username(self.username)
        if username_reason:
            raise ValueError(username_reason)
        password_reason = check_password(
            self.password, username=self.username, email=str(self.email)
        )
        if password_reason:
            raise ValueError(password_reason)
        return self


class LoginRequest(CaptchaFields):
    """POST /auth/login 请求体。

    captcha_token 仅当该账号或 IP 已有失败记录时才要求（见 routes/auth 的登录逻辑）。
    """

    identifier: str = Field(..., min_length=1, description="邮箱或用户名")
    password: str = Field(..., min_length=1)


class PasswordForgotRequest(CaptchaFields):
    """POST /auth/password/forgot 请求体"""

    email: EmailStr


class PasswordResetRequest(CaptchaFields):
    """POST /auth/password/reset 请求体"""

    token: str = Field(..., min_length=1, description="邮件里的一次性重置令牌")
    new_password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_BYTES)

    @model_validator(mode="after")
    def _validate_password(self) -> "PasswordResetRequest":
        reason = check_password(self.new_password)
        if reason:
            raise ValueError(reason)
        return self


class PasswordChangeRequest(CaptchaFields):
    """POST /auth/password/change 请求体（已登录）"""

    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_BYTES)

    @model_validator(mode="after")
    def _validate_password(self) -> "PasswordChangeRequest":
        reason = check_password(self.new_password)
        if reason:
            raise ValueError(reason)
        return self


class AccountDeleteRequest(CaptchaFields):
    """POST /auth/account/delete 请求体"""

    password: str = Field(..., min_length=1)
    confirm_username: str = Field(
        ..., min_length=1, description="输入自己的用户名以二次确认"
    )


class AccountDeleteCancelRequest(CaptchaFields):
    """POST /auth/account/delete/cancel 请求体

    冷静期内账号 `is_active=false` 无法走正常登录，因此撤销注销用「标识 + 密码」
    直接自证身份，签发的是「撤销操作」而非会话。

    **必须带人机校验字段**：这是一个**匿名可达、以密码为唯一凭证**的端点，
    与「登录」同属撞库目标。少了 captcha 就等于给爆破留了一条不需要过闸门的旁路。
    """

    identifier: str = Field(..., min_length=1, description="邮箱或用户名")
    password: str = Field(..., min_length=1)


class UserPublic(BaseModel):
    """对外暴露的用户信息（绝不含 password_hash）"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    username: str
    is_active: bool
    # 邮箱是否已验证（阶段 5 确认邮件）。注册不做阻塞式校验，因此 false 是正常状态，
    # 仅用于向用户提示「收不到确认邮件 = 邮箱可能填错了」。
    email_verified: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: Any) -> "UserPublic":
        """ORM -> 对外模型。手写映射而非 model_validate：显式列出字段，
        保证将来给 User 加任何敏感列都不会被自动带出去。
        """
        return cls(
            id=str(user.id),
            email=user.email,
            username=user.username,
            is_active=user.is_active,
            email_verified=user.email_verified_at is not None,
            created_at=user.created_at,
        )


class EmailConfirmRequest(BaseModel):
    """POST /auth/email/confirm：确认邮件链接里的凭据"""

    token: str = Field(..., min_length=1, description="确认邮件链接中的令牌")


class TokenResponse(BaseModel):
    """POST /auth/login 响应：access 走响应体，refresh 走 Set-Cookie"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="access token 有效期（秒）")
    user: UserPublic


class RefreshTokenResponse(BaseModel):
    """POST /auth/refresh 响应"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="access token 有效期（秒）")


class LogoutResponse(BaseModel):
    """POST /auth/logout 响应"""

    status: str
    message: str


class SimpleMessageResponse(BaseModel):
    """通用「操作已完成」响应（找回/重置/改密/注销等）"""

    status: str
    message: str


class CaptchaConfigResponse(BaseModel):
    """GET /auth/captcha 响应：前端据此初始化人机校验组件。

    `vid` 是验证单元的公开 id（前端初始化必须），`key`（secretkey）**绝不外泄**。
    """

    enabled: bool = Field(..., description="当前是否开启人机校验")
    provider: str = Field(..., description="off | vaptcha")
    vid: Optional[str] = Field(None, description="VAPTCHA 验证单元 id（公开）")


class AuthSessionItem(BaseModel):
    """一个登录设备会话（GET /auth/sessions）"""

    id: str
    current: bool = Field(False, description="是否为当前请求所属会话")
    user_agent: Optional[str] = Field(None, description="设备/浏览器标识摘要")
    ip_hint: Optional[str] = Field(None, description="IP 哈希前缀（不可反查）")
    last_seen_at: Optional[datetime] = None
    created_at: datetime
    expires_at: datetime


class AuthSessionListResponse(BaseModel):
    """GET /auth/sessions 响应"""

    items: List[AuthSessionItem]
    count: int


class SessionRevokeResponse(BaseModel):
    """DELETE /auth/sessions[...] 响应"""

    status: str
    revoked: int = Field(..., description="本次撤销的会话数")
    message: str
