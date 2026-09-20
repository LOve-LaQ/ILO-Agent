# Auth Routes - 注册 / 登录 / 刷新 / 登出 / 找回密码 / 会话管理
"""阶段 2 权限边界 + 阶段 5 入口加固的入口路由。

Token 分工：
- access token（默认 30 分钟）走响应体，前端只存内存，XSS 拿不到长效凭证；
- refresh token（默认 14 天）只放 httpOnly Cookie，且 path 限定 /api/v1/auth，
  既防 JS 读取，也不会随业务请求自动携带。

阶段 5 在入口处叠加四层闸门（互为补充，不把任何单一手段当唯一防线）：
1. 限流：IP 维度管广度、账号维度管「对单一目标的深度」（core/rate_limit）；
2. 登录失败锁定：账号维度计数，话术**绝不暴露账号是否存在**（services/login_guard）；
3. 人机校验：注册 / 找回 / 重置**强制**，登录仅在已有失败记录时要求；
4. 用户名规范与密码策略（schemas/auth.py 的 model_validator）。

用户身份的唯一真相源是 access token 的 `sub`（users.id），请求体里的 user_id 不被信任。
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

import jwt
from fastapi import APIRouter, Cookie, Depends, Request, Response
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from src.api.deps import CurrentUser, DbSession, OptionalUser, _extract_bearer_token
from src.core.config import settings
from src.core.errors import ERROR_RESPONSES, ILOException
from src.core.net import client_ip, hash_ip, subject_fingerprint
from src.core.password_policy import check_password
from src.core.rate_limit import (
    CAPTCHA_IP,
    EMAIL_RESEND_IP,
    FORGOT_EMAIL,
    FORGOT_IP,
    LOGIN_ACCOUNT,
    LOGIN_IP,
    REFRESH_IP,
    REGISTER_IP,
    enforce,
    rate_limit,
)
from src.core.security import (
    access_token_expires_in,
    decode_token,
    hash_password,
    verify_password,
)
from src.models.consent import ConsentRecord
from src.models.user import User
from src.schemas.auth import (
    AccountDeleteCancelRequest,
    AccountDeleteRequest,
    AuthSessionItem,
    AuthSessionListResponse,
    CaptchaConfigResponse,
    EmailConfirmRequest,
    LoginRequest,
    LogoutResponse,
    PasswordChangeRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RefreshTokenResponse,
    RegisterRequest,
    SessionRevokeResponse,
    SimpleMessageResponse,
    TokenResponse,
    UserPublic,
)
from src.services.activity_service import log_activity
from src.services.captcha_service import CaptchaError, is_captcha_enabled, verify_captcha
from src.services.email_service import send_email
from src.services.email_verification_service import (
    resolve_token as resolve_email_verify_token,
    send_verification_email,
)
from src.services.login_guard import (
    clear_failures,
    current_failures,
    is_locked,
    record_failure,
)
from src.services.password_reset_service import issue_token, resolve_token
from src.services.session_service import (
    get_session_for_user,
    list_active_sessions,
    revoke_all_sessions,
    revoke_by_jti,
    revoke_session,
    rotate_session,
    start_session,
)

router = APIRouter(prefix="/auth", tags=["Auth"], responses=ERROR_RESPONSES)

# refresh Cookie 只在认证接口间流转，避免被所有业务请求裹挟
REFRESH_COOKIE_PATH = "/api/v1/auth"

# 登录限流 / 锁定统一话术：绝不能出现「账号已锁定」，否则等于确认账号存在
_LOCKED_MESSAGE = "登录尝试过于频繁，请稍后再试。"

# 邮箱确认统一话术：不区分「签名不对 / 类型不对 / 已过期 / 账号已注销」
_EMAIL_CONFIRM_INVALID_MESSAGE = "确认链接无效或已过期，请登录后在账户设置里重新发送。"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_public(user: User) -> UserPublic:
    """ORM -> 对外模型（绝不外泄 password_hash）"""
    return UserPublic.from_user(user)


def _set_refresh_cookie(response: Response, token: str, expires_at: datetime) -> None:
    max_age = max(0, int((expires_at - _now()).total_seconds()))
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
        domain=settings.cookie_domain or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=REFRESH_COOKIE_PATH,
        domain=settings.cookie_domain or None,
    )


def _decode(token: str, expected_type: str) -> dict:
    """解码 token；把 JWT 异常翻译成业务 401 错误"""
    try:
        return decode_token(token, expected_type=expected_type)  # type: ignore[arg-type]
    except jwt.ExpiredSignatureError:
        raise ILOException("TOKEN_EXPIRED", "登录状态已过期，请重新登录。", status_code=401)
    except jwt.PyJWTError:
        raise ILOException("TOKEN_INVALID", "登录凭证无效，请重新登录。", status_code=401)


def _access_sid(request: Request) -> Optional[str]:
    """当前请求 access token 里的 sid（用于「是否为当前会话」判定）"""
    token = _extract_bearer_token(request)
    if not token:
        return None
    try:
        return decode_token(token, expected_type="access").get("sid")
    except jwt.PyJWTError:
        return None


def _resolve_user_by_identifier(db, identifier: str) -> Optional[User]:
    return (
        db.execute(
            select(User).where(
                or_(User.email == identifier.lower(), User.username == identifier)
            )
        )
        .scalars()
        .first()
    )


def _require_captcha(
    token: Optional[str],
    request: Request,
    *,
    event_target: str,
    knock: Optional[str] = None,
    dfu: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """统一人机校验入口：开启校验时强制执行，失败按 fail-closed 拒绝并埋点。

    关闭校验（开发环境未配置 provider）时直接放过 —— 生产环境由启动自检
    （assert_security_config）保证 provider 一定是开的。
    knock/dfu/ip 是 VAPTCHA V4 中被 token 签名覆盖的伴随数据，必须随 token 一起回传。
    """
    if not is_captcha_enabled():
        return
    try:
        verify_captcha(token, request=request, knock=knock, dfu=dfu, ip=ip)
    except CaptchaError:
        log_activity(
            "security_captcha_failed",
            target_type="endpoint",
            target_id=event_target,
            request=request,
        )
        raise


def _record_consent(db, user: User, request: Request) -> None:
    """记录用户对隐私政策 / 用户协议的同意（记版本，不是布尔值）"""
    ip = hash_ip(client_ip(request))
    for document in ("privacy", "terms"):
        db.add(
            ConsentRecord(
                user_id=user.id,
                document=document,
                version=settings.legal_document_version,
                ip_hash=ip,
            )
        )
    db.commit()


def _reset_email_body(username: str, link: str) -> str:
    return (
        f"你好 {username}：\n\n"
        "我们收到了重置 ILO 账号密码的请求。点击下面的链接设置新密码"
        f"（{settings.password_reset_expire_minutes} 分钟内有效，且只能使用一次）：\n\n"
        f"{link}\n\n"
        "如果这不是你本人的操作，请忽略此邮件，你的密码不会被更改。\n"
    )


def _deletion_email_body(username: str, grace_days: int) -> str:
    return (
        f"你好 {username}：\n\n"
        f"我们已受理你的账号注销申请。账号将在 {grace_days} 天后被永久删除。\n\n"
        "如果你改变主意，可在冷静期内重新登录并在「账号安全」中撤销注销申请。\n"
        "若此次申请并非你本人发起，请立即修改密码并联系支持。\n"
    )


# ==================== 注册 / 登录 / 刷新 / 登出 ====================


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=201,
    dependencies=[Depends(rate_limit(REGISTER_IP))],
)
def register(
    data: RegisterRequest, request: Request, response: Response, db: DbSession
) -> TokenResponse:
    """注册并直接进入登录态（返回 access，种下 refresh Cookie 与会话行）"""
    _require_captcha(
        data.captcha_token,
        request,
        event_target="register",
        knock=data.captcha_knock,
        dfu=data.captcha_dfu,
        ip=data.captcha_ip,
    )

    email = str(data.email).strip().lower()
    username = data.username.strip()

    existing = (
        db.execute(select(User).where(or_(User.email == email, User.username == username)))
        .scalars()
        .first()
    )
    if existing is not None:
        if existing.email == email:
            raise ILOException("EMAIL_TAKEN", "该邮箱已被注册，请直接登录。", status_code=409)
        raise ILOException("USERNAME_TAKEN", "该用户名已被占用，换一个试试。", status_code=409)

    user = User(
        email=email,
        username=username,
        password_hash=hash_password(data.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # 并发注册同名账号时唯一约束兜底
        db.rollback()
        raise ILOException("USER_EXISTS", "该邮箱或用户名已被注册。", status_code=409)
    db.refresh(user)

    # 同意记录（用户勾选了协议时）；写版本号，供协议改版时判定是否需要重新获取
    if data.accept_terms:
        _record_consent(db, user, request)

    access_token, refresh_token, refresh_expires_at = start_session(db, user, request)
    _set_refresh_cookie(response, refresh_token, refresh_expires_at)
    # 日志只记 id：username/email 属 PII，不应出现在日志
    logger.info(f"User registered: id={user.id}")

    # 行为溯源：注册即登录，这条流水是用户行为链的起点
    log_activity(
        "register",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )

    # 确认邮件：**非阻塞**（send_email 内部吞异常）。注册不校验邮箱真实性，
    # 因此用这封信让「填错邮箱」提前暴露 —— 收不到就说明地址可能不对，
    # 免得等到真忘了密码、走找回流程时才发现。
    send_verification_email(user)

    return TokenResponse(
        access_token=access_token,
        expires_in=access_token_expires_in(),
        user=_to_public(user),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit(LOGIN_IP))],
)
def login(
    data: LoginRequest, request: Request, response: Response, db: DbSession
) -> TokenResponse:
    """邮箱或用户名 + 密码登录"""
    identifier = data.identifier.strip()

    # 账号维度限流：与 IP 维度互补，拦住「换 IP 打同一个账号」的撞库
    enforce(LOGIN_ACCOUNT, subject_fingerprint(identifier), message=_LOCKED_MESSAGE)

    locked, retry_after = is_locked(identifier)
    if locked:
        raise ILOException(
            "RATE_LIMITED",
            _LOCKED_MESSAGE,
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    # 已有失败记录时才要求人机校验：正常用户不该被每次登录打扰
    if is_captcha_enabled() and current_failures(identifier) > 0:
        _require_captcha(
            data.captcha_token,
            request,
            event_target="login",
            knock=data.captcha_knock,
            dfu=data.captcha_dfu,
            ip=data.captcha_ip,
        )

    user = _resolve_user_by_identifier(db, identifier)

    # 账号不存在与密码错误返回同一提示，避免账号枚举
    if user is None or not verify_password(data.password, user.password_hash):
        record_failure(identifier)
        raise ILOException(
            "INVALID_CREDENTIALS", "邮箱/用户名或密码不正确。", status_code=401
        )
    if not user.is_active:
        if user.deletion_requested_at is not None:
            raise ILOException(
                "ACCOUNT_PENDING_DELETION",
                "该账号已申请注销，可在冷静期内撤销注销后继续使用。",
                status_code=403,
            )
        raise ILOException("USER_DISABLED", "账号已被停用。", status_code=403)

    clear_failures(identifier)
    access_token, refresh_token, refresh_expires_at = start_session(db, user, request)
    _set_refresh_cookie(response, refresh_token, refresh_expires_at)
    logger.info(f"User logged in: id={user.id}")

    log_activity(
        "login",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )

    return TokenResponse(
        access_token=access_token,
        expires_in=access_token_expires_in(),
        user=_to_public(user),
    )


@router.post(
    "/refresh",
    response_model=RefreshTokenResponse,
    dependencies=[Depends(rate_limit(REFRESH_IP))],
)
def refresh(
    request: Request,
    response: Response,
    db: DbSession,
    refresh_token: Annotated[
        Optional[str], Cookie(alias=settings.refresh_cookie_name)
    ] = None,
) -> RefreshTokenResponse:
    """用 httpOnly Cookie 里的 refresh token 轮换出新的 access（并换发 refresh）"""
    if not refresh_token:
        raise ILOException("REFRESH_TOKEN_MISSING", "登录状态已失效，请重新登录。", status_code=401)

    payload = _decode(refresh_token, "refresh")
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise ILOException("TOKEN_INVALID", "登录凭证无效，请重新登录。", status_code=401)

    user = db.get(User, user_id)
    if user is None:
        raise ILOException("USER_NOT_FOUND", "账号不存在，请重新登录。", status_code=401)
    if not user.is_active:
        # 注销冷静期内的账号不再续期；停用账号同理
        raise ILOException("USER_DISABLED", "账号已被停用。", status_code=403)

    # 轮换：正常路径会换发新 refresh；并发宽限路径只补发 access（refresh_token=None）
    result = rotate_session(db, user=user, jti=payload.get("jti"), request=request)
    if result.get("refresh_token"):
        _set_refresh_cookie(response, result["refresh_token"], result["refresh_expires_at"])

    return RefreshTokenResponse(
        access_token=result["access_token"], expires_in=access_token_expires_in()
    )


@router.post("/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    response: Response,
    user: OptionalUser,
    db: DbSession,
    refresh_token: Annotated[
        Optional[str], Cookie(alias=settings.refresh_cookie_name)
    ] = None,
) -> LogoutResponse:
    """撤销当前会话并清掉 refresh Cookie（access token 由前端丢弃）

    刻意不强制登录：access 过期后用户仍必须能登出，否则前端会卡在
    「登不出去、又提示重新登录」的死角。拿不到用户时只是少记一条流水。
    """
    if refresh_token:
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
            revoke_by_jti(db, payload.get("jti"))
        except jwt.PyJWTError:
            # 凭证已过期/损坏也要能登出：清 Cookie 本身就能完成「登出」
            pass

    _clear_refresh_cookie(response)

    if user is not None:
        log_activity(
            "logout",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            request=request,
        )

    return LogoutResponse(status="ok", message="已退出登录。")


# ==================== 人机校验配置 ====================


@router.get("/captcha", response_model=CaptchaConfigResponse)
def captcha_config() -> CaptchaConfigResponse:
    """前端初始化人机校验组件所需配置（vid 公开，key 保密，绝不返回）"""
    provider = (settings.captcha_provider or "off").strip().lower()
    return CaptchaConfigResponse(
        enabled=is_captcha_enabled(),
        provider=provider,
        vid=(settings.vaptcha_vid or None),
    )


# ==================== 找回 / 重置 / 修改密码 ====================


@router.post(
    "/password/forgot",
    response_model=SimpleMessageResponse,
    dependencies=[Depends(rate_limit(FORGOT_IP))],
)
def password_forgot(
    data: PasswordForgotRequest, request: Request, db: DbSession
) -> SimpleMessageResponse:
    """申请找回密码：发一次性重置链接（防枚举 + 防轰炸）"""
    _require_captcha(
        data.captcha_token,
        request,
        event_target="password_forgot",
        knock=data.captcha_knock,
        dfu=data.captcha_dfu,
        ip=data.captcha_ip,
    )

    email = str(data.email).strip().lower()
    enforce(
        FORGOT_EMAIL,
        subject_fingerprint(email),
        message="该邮箱的找回请求过于频繁，请稍后再试。",
    )

    user = (
        db.execute(select(User).where(User.email == email)).scalars().first()
    )
    if user is not None and user.deleted_at is None and user.is_active:
        token = issue_token(db, user)
        if token:
            link = f"{settings.frontend_base_url.rstrip('/')}/reset-password?token={token}"
            send_email(
                user.email,
                "重置你的 ILO 密码",
                _reset_email_body(user.username, link),
                purpose="password_reset",
            )

    # 无论邮箱是否存在，响应完全一致 —— 这是防枚举的关键
    return SimpleMessageResponse(
        status="ok",
        message=(
            "如果该邮箱已注册，我们已经发送了重置链接，请查收"
            f"（{settings.password_reset_expire_minutes} 分钟内有效）。"
        ),
    )


@router.post(
    "/password/reset",
    response_model=SimpleMessageResponse,
    dependencies=[Depends(rate_limit(CAPTCHA_IP))],
)
def password_reset(
    data: PasswordResetRequest, request: Request, db: DbSession
) -> SimpleMessageResponse:
    """用一次性令牌重置密码；成功后撤销该用户全部会话"""
    _require_captcha(
        data.captcha_token,
        request,
        event_target="password_reset",
        knock=data.captcha_knock,
        dfu=data.captcha_dfu,
        ip=data.captcha_ip,
    )

    resolved = resolve_token(db, data.token)
    if resolved is None:
        raise ILOException(
            "RESET_TOKEN_INVALID", "重置链接无效或已过期，请重新申请。", status_code=400
        )
    row, user = resolved

    user.password_hash = hash_password(data.new_password)
    row.used_at = _now()
    db.commit()

    # 密码变了，旧凭证一律作废；顺手清掉该账号的登录失败计数
    revoke_all_sessions(db, user.id)
    clear_failures(user.email)
    clear_failures(user.username)
    logger.info(f"Password reset completed: id={user.id}")

    log_activity(
        "password_reset",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )
    return SimpleMessageResponse(status="ok", message="密码已重置，请使用新密码登录。")


@router.post("/password/change", response_model=SimpleMessageResponse)
def password_change(
    data: PasswordChangeRequest, request: Request, user: CurrentUser, db: DbSession
) -> SimpleMessageResponse:
    """已登录状态修改密码；撤销**除当前会话外**的全部会话"""
    _require_captcha(
        data.captcha_token,
        request,
        event_target="password_change",
        knock=data.captcha_knock,
        dfu=data.captcha_dfu,
        ip=data.captcha_ip,
    )

    if not verify_password(data.current_password, user.password_hash):
        raise ILOException("INVALID_CREDENTIALS", "当前密码不正确。", status_code=400)
    if data.current_password == data.new_password:
        raise ILOException("PASSWORD_UNCHANGED", "新密码不能与当前密码相同。", status_code=400)

    # 这里能拿到身份信息，做一次「密码不得与用户名/邮箱重合」的完整校验
    reason = check_password(data.new_password, username=user.username, email=user.email)
    if reason:
        raise ILOException("WEAK_PASSWORD", reason, status_code=400)

    user.password_hash = hash_password(data.new_password)
    db.commit()

    revoked = revoke_all_sessions(db, user.id, except_sid=_access_sid(request))
    clear_failures(user.email)
    clear_failures(user.username)
    logger.info(f"Password changed: id={user.id} revoked_sessions={revoked}")

    log_activity(
        "password_change",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={"revoked_sessions": revoked},
        request=request,
    )
    return SimpleMessageResponse(status="ok", message="密码已修改，其他设备已下线。")


# ==================== 账号注销（冷静期） ====================


@router.post("/account/delete", response_model=SimpleMessageResponse)
def account_delete(
    data: AccountDeleteRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbSession,
) -> SimpleMessageResponse:
    """申请注销账号：进入冷静期、立即停用并撤销全部会话、发确认邮件"""
    _require_captcha(
        data.captcha_token,
        request,
        event_target="account_delete",
        knock=data.captcha_knock,
        dfu=data.captcha_dfu,
        ip=data.captcha_ip,
    )

    if not verify_password(data.password, user.password_hash):
        raise ILOException("INVALID_CREDENTIALS", "密码不正确。", status_code=400)
    if data.confirm_username.strip() != user.username:
        raise ILOException(
            "CONFIRM_MISMATCH", "用户名不匹配，请准确输入你的用户名以确认。", status_code=400
        )

    user.deletion_requested_at = _now()
    user.is_active = False
    db.commit()

    revoke_all_sessions(db, user.id)
    _clear_refresh_cookie(response)

    grace = max(0, settings.account_deletion_grace_days)
    send_email(
        user.email,
        "ILO 账号注销申请已受理",
        _deletion_email_body(user.username, grace),
        purpose="account_deletion",
    )
    logger.info(f"Account deletion requested: id={user.id}")

    log_activity(
        "account_delete_requested",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )
    return SimpleMessageResponse(
        status="ok",
        message=f"已受理注销申请。账号将在 {grace} 天后被永久删除，期间登录可撤销。",
    )


@router.post("/account/delete/cancel", response_model=SimpleMessageResponse)
def account_delete_cancel(
    data: AccountDeleteCancelRequest, request: Request, db: DbSession
) -> SimpleMessageResponse:
    """冷静期内撤销注销申请（账号已停用，故用「标识 + 密码」自证身份）"""
    user = _resolve_user_by_identifier(db, data.identifier.strip())
    if user is None or not verify_password(data.password, user.password_hash):
        raise ILOException(
            "INVALID_CREDENTIALS", "邮箱/用户名或密码不正确。", status_code=401
        )
    if user.deletion_requested_at is None:
        raise ILOException(
            "NO_DELETION_PENDING", "该账号没有待处理的注销申请。", status_code=400
        )

    user.deletion_requested_at = None
    user.deleted_at = None
    user.is_active = True
    db.commit()
    logger.info(f"Account deletion cancelled: id={user.id}")

    log_activity(
        "account_delete_cancelled",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )
    return SimpleMessageResponse(status="ok", message="已撤销注销申请，账号已恢复。")


# ==================== 登录设备（会话）管理 ====================


@router.get("/sessions", response_model=AuthSessionListResponse)
def get_sessions(
    request: Request, user: CurrentUser, db: DbSession
) -> AuthSessionListResponse:
    """列出当前登录设备（与 /me/sessions 的学习记录是两回事）"""
    current_sid = _access_sid(request)
    rows = list_active_sessions(db, user.id)
    items = [
        AuthSessionItem(
            id=str(row.id),
            current=(current_sid is not None and str(row.id) == current_sid),
            user_agent=row.user_agent,
            # 只给哈希前缀，够用来区分设备，又不提供反查能力
            ip_hint=(row.ip_hash[:8] if row.ip_hash else None),
            last_seen_at=row.last_seen_at,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        for row in rows
    ]
    return AuthSessionListResponse(items=items, count=len(items))


@router.delete("/sessions/{session_id}", response_model=SessionRevokeResponse)
def revoke_one_session(
    session_id: str, request: Request, user: CurrentUser, db: DbSession
) -> SessionRevokeResponse:
    """下线指定设备。不属于当前用户的会话一律 404（不泄露会话是否存在）"""
    try:
        sid = uuid.UUID(session_id)
    except (TypeError, ValueError):
        raise ILOException("SESSION_NOT_FOUND", "会话不存在。", status_code=404)

    session = get_session_for_user(db, user.id, sid)
    if session is None:
        raise ILOException("SESSION_NOT_FOUND", "会话不存在。", status_code=404)

    revoke_session(db, session)
    log_activity(
        "session_revoked",
        user_id=user.id,
        target_type="session",
        target_id=str(sid),
        request=request,
    )
    return SessionRevokeResponse(status="ok", revoked=1, message="该设备已下线。")


@router.delete("/sessions", response_model=SessionRevokeResponse)
def revoke_other_sessions(
    request: Request, user: CurrentUser, db: DbSession
) -> SessionRevokeResponse:
    """下线除当前设备外的全部设备"""
    revoked = revoke_all_sessions(db, user.id, except_sid=_access_sid(request))
    log_activity(
        "session_revoked",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={"revoked": revoked},
        request=request,
    )
    return SessionRevokeResponse(status="ok", revoked=revoked, message="其他设备已全部下线。")


# ==================== 邮箱确认 ====================


@router.post("/email/confirm", response_model=SimpleMessageResponse)
def email_confirm(
    data: EmailConfirmRequest, request: Request, db: DbSession
) -> SimpleMessageResponse:
    """确认邮箱（点击确认邮件里的链接）。

    **幂等**：用户常常连点两三次，重复点击一律返回同样的成功话术。
    **不限流**：令牌是 HS256 签名的 JWT，伪造不出来；重放旧令牌只会命中
    「已验证」分支，没有可被放大的副作用。
    """
    subject = resolve_email_verify_token(data.token)
    if subject is None:
        raise ILOException(
            "EMAIL_CONFIRM_INVALID", _EMAIL_CONFIRM_INVALID_MESSAGE, status_code=400
        )
    try:
        user_id = uuid.UUID(subject)
    except (TypeError, ValueError):
        raise ILOException(
            "EMAIL_CONFIRM_INVALID", _EMAIL_CONFIRM_INVALID_MESSAGE, status_code=400
        )

    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        # 已注销（匿名壳）或正在冷静期内的账号都不应再对外产生任何动作
        raise ILOException(
            "EMAIL_CONFIRM_INVALID", _EMAIL_CONFIRM_INVALID_MESSAGE, status_code=400
        )

    if user.email_verified_at is None:
        user.email_verified_at = _now()
        db.commit()
        log_activity(
            "email_verified",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            request=request,
        )

    return SimpleMessageResponse(status="ok", message="邮箱已确认，感谢你的配合。")


@router.post(
    "/email/resend",
    response_model=SimpleMessageResponse,
    dependencies=[Depends(rate_limit(EMAIL_RESEND_IP))],
)
def email_resend(user: CurrentUser) -> SimpleMessageResponse:
    """重发确认邮件（需登录）。

    必须给用户一个自助重发的入口：这封信是**唯一**能发现「邮箱填错了」的手段，
    一旦它丢了（进了垃圾箱、被误删），用户就再也无法确认 —— 那这封信等于白设计。
    """
    if user.email_verified_at is not None:
        return SimpleMessageResponse(
            status="ok", message="这个邮箱已经确认过了，无需重复确认。"
        )

    send_verification_email(user)
    return SimpleMessageResponse(
        status="ok",
        message=(
            "确认邮件已发送，请查收"
            f"（{settings.email_verification_expire_hours} 小时内有效）。"
        ),
    )


# ==================== 当前用户 ====================


@router.get("/me", response_model=UserPublic)
def read_me(user: CurrentUser) -> UserPublic:
    """返回当前登录用户（前端刷新页面后用它校验 access token 是否仍有效）"""
    return _to_public(user)
