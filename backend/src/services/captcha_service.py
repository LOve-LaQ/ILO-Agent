# Captcha - 人机校验（VAPTCHA V4）
"""阶段 5：注册 / 找回密码 / 重置密码的入口闸门。

**为什么服务端必须二次校验**：前端拿到 token 只代表「这个浏览器验证通过了」。
攻击者完全可以跳过前端直接 `POST /auth/register`，前端校验一次都不会执行。
token 是「证据」，验证证据真伪只能在服务端做 —— 这是这一层唯一有意义的位置。
所以这里不做「前端传了 token 就放行」的宽容处理。

**V4 的调用契约（实测确认）**：POST JSON 到 `VAPTCHA_VERIFY_URL`
（`https://v41.vaptcha.com/api/verify`），请求体为 `{vid, vkey, token, knock, dfu, ip}`
—— 密钥字段名是 **`vkey`**（不是 V3 的 `secretkey`，也不是 form 表单）。
`knock`/`dfu` 来自前端 `validate()`，且被 token 签名覆盖，必须原样透传，
否则上游无法完成验签。判定只看 `data.result===true` 且 `data.code===0`；
`code` 语义：0 通过 / 1 鉴权不通过 / 2 token 过期 / 3 其它失败。
端点地址做成配置项，换版本或换厂商只改 .env。

**校验失败一律 fail-closed**：网络超时、上游异常、响应无法解析，全部按「未通过」
处理。取舍很明确 —— 宁可让用户重试一次，也不能让一个绕过前端的脚本凭「校验服务不可用」
直接注册成功。
"""

from typing import Optional

import httpx
from loguru import logger

from src.core.config import settings
from src.core.errors import ILOException

# 上游校验的超时：这是注册链路里的同步阻塞点，超过这个时间用户已经在怀疑卡死了
VERIFY_TIMEOUT_SECONDS = 5.0


class CaptchaError(ILOException):
    """人机校验未通过（统一 400，前端据 code 提示重新验证）"""

    def __init__(self, message: str, *, code: str = "CAPTCHA_FAILED"):
        super().__init__(code, message, status_code=400)


class CaptchaVerifier:
    """人机校验适配器基类

    换厂商（极验 / 阿里云验证码）时只需新增一个子类并改 `CAPTCHA_PROVIDER`，
    路由侧调用点不变。
    """

    name = "base"

    def verify(
        self,
        token: Optional[str],
        *,
        remote_ip: Optional[str] = None,
        ip: Optional[str] = None,
        knock: Optional[str] = None,
        dfu: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    def _require_token(self, token: Optional[str]) -> str:
        """token 缺失即拒绝：绝不能因为「前端没传」就默认放行"""
        value = (token or "").strip()
        if not value:
            raise CaptchaError("请先完成人机验证。", code="CAPTCHA_REQUIRED")
        return value


class NullVerifier(CaptchaVerifier):
    """关闭校验（`CAPTCHA_PROVIDER=off`）

    只在非生产环境允许，生产环境由 `assert_security_config()` 在启动时直接拦下 ——
    否则一次环境变量误配就能让注册入口彻底裸奔。
    """

    name = "off"

    def verify(
        self,
        token: Optional[str],
        *,
        remote_ip: Optional[str] = None,
        ip: Optional[str] = None,
        knock: Optional[str] = None,
        dfu: Optional[str] = None,
    ) -> None:
        return


class VaptchaVerifier(CaptchaVerifier):
    """VAPTCHA V4 服务端二次校验"""

    name = "vaptcha"

    def verify(
        self,
        token: Optional[str],
        *,
        remote_ip: Optional[str] = None,
        ip: Optional[str] = None,
        knock: Optional[str] = None,
        dfu: Optional[str] = None,
    ) -> None:
        value = self._require_token(token)

        vid = (settings.vaptcha_vid or "").strip()
        vkey = (settings.vaptcha_key or "").strip()
        if not vid or not vkey:
            # 缺配置时不能静默放行：那等于「忘了配 = 没防护」，而忘配恰恰最容易发生
            logger.error("[ERROR] 未配置 VAPTCHA_VID / VAPTCHA_KEY，人机校验无法执行")
            raise CaptchaError("人机校验服务未配置，请联系管理员。", code="CAPTCHA_MISCONFIGURED")

        # V4 契约：JSON 体，密钥字段名是 vkey；knock/dfu 被 token 签名覆盖，必须一并提交
        payload = {"vid": vid, "vkey": vkey, "token": value}
        # 签名公式是 HMAC(timestamp.ip.dfu.knock, vkey)——ip 也是被签名覆盖的字段，
        # 必须与「验证时 VAPTCHA 看到的那个 IP」一致，否则上游验签必然失败。
        # 前端 validate() 回传的 ip 正是签名所用 IP，因此优先采用；它只会让验签「更可能对」，
        # 改它只会导致验签失败、无法伪造（签名仍需 vkey），所以优先采用不构成越权风险。
        # 兜底才用服务端看到的来源 IP（本机直连时是 127.0.0.1，与签名 IP 不同，故只能兜底）。
        effective_ip = (ip or "").strip() or remote_ip
        if effective_ip:
            payload["ip"] = effective_ip
        if knock:
            payload["knock"] = knock
        if dfu:
            payload["dfu"] = dfu

        try:
            response = httpx.post(
                settings.vaptcha_verify_url,
                json=payload,
                timeout=VERIFY_TIMEOUT_SECONDS,
            )
            body = response.json()
        except Exception as e:  # noqa: BLE001 - 网络/解析异常统一按未通过处理
            logger.warning(f"[WARN] 人机校验请求失败（按未通过处理）: {e}")
            raise CaptchaError("人机校验服务暂时不可用，请稍后重试。")

        data = body.get("data") or {}
        try:
            data_code = int(data.get("code"))
        except (TypeError, ValueError):
            data_code = -1
        # 严格口径：data.result 为真且 data.code==0 才算通过（data.result 是真正的布尔判定）
        passed = data.get("result") is True and data_code == 0

        if not passed:
            # 上游判定原文进日志：联调阶段 URL/字段名/vkey 不对时，这一行是唯一线索。
            # 只含上游判定结果，不含用户凭证。
            logger.warning(
                f"[WARN] 人机校验未通过: code={data.get('code')} "
                f"note={data.get('note')} msg={body.get('msg')}"
            )
            raise CaptchaError("人机验证未通过，请重新验证。")


def get_captcha_verifier() -> CaptchaVerifier:
    """按配置产出校验器（每请求调用一次：provider 可从环境变量热改）"""
    provider = (settings.captcha_provider or "").strip().lower()
    if provider == "vaptcha":
        return VaptchaVerifier()
    return NullVerifier()


def is_captcha_enabled() -> bool:
    """当前是否真的在做校验（前端据此决定要不要渲染验证组件）"""
    return not isinstance(get_captcha_verifier(), NullVerifier)


def verify_captcha(
    token: Optional[str],
    *,
    request=None,
    ip: Optional[str] = None,
    knock: Optional[str] = None,
    dfu: Optional[str] = None,
) -> None:
    """路由侧统一入口：校验不通过抛 400

    `ip` 是前端 `validate()` 回传的「签名所用 IP」，优先于服务端来源 IP（详见 VaptchaVerifier）。
    """
    from src.core.net import client_ip

    get_captcha_verifier().verify(
        token,
        remote_ip=client_ip(request) if request else None,
        ip=ip,
        knock=knock,
        dfu=dfu,
    )


def assert_security_config() -> None:
    """启动期安全自检（生产环境配错就直接拒绝启动）

    这类检查必须在**启动时**做而不是等第一次注册才发现：配错的代价是整个注册入口
    失去防护，越早暴露越好。开发环境只告警，不阻塞本地跑起来。
    """
    is_production = (settings.environment or "").lower() in ("production", "prod")

    if settings.captcha_provider.strip().lower() == "vaptcha" and not (
        settings.vaptcha_vid and settings.vaptcha_key
    ):
        message = (
            "CAPTCHA_PROVIDER=vaptcha 但未配置 VAPTCHA_VID / VAPTCHA_KEY，"
            "人机校验无法执行。"
        )
        if is_production:
            raise RuntimeError(message)
        logger.warning(f"[WARN] {message}（开发环境将以 off 降级运行）")
        settings.captcha_provider = "off"

    # 生产环境严禁「静默无验证」：显式 off 或任何解析为 NullVerifier 的取值一律拒绝启动。
    # 放在 vaptcha 分支之后：生产缺 key 会先抛出上面那条更具体的错误；这里兜住
    # 「图省事把 provider 改成 off」这条最容易发生、且原本不会报错的误配路径 ——
    # 它比缺 key 更隐蔽：没有任何告警，注册入口就是裸的。
    if is_production and not is_captcha_enabled():
        raise RuntimeError(
            "CAPTCHA_PROVIDER=off 只允许在非生产环境运行；生产环境必须启用人机校验"
            "（设置 CAPTCHA_PROVIDER=vaptcha 并配置 VAPTCHA_VID / VAPTCHA_KEY）。"
        )

    if settings.email_provider.strip().lower() == "console":
        message = "EMAIL_PROVIDER=console 只把邮件写入数据库，不会真正投递。"
        if is_production:
            raise RuntimeError(f"{message} 生产环境请配置 EMAIL_PROVIDER=smtp。")
        logger.info(f"[INFO] {message}（开发环境）")


__all__ = [
    "CaptchaError",
    "CaptchaVerifier",
    "NullVerifier",
    "VaptchaVerifier",
    "assert_security_config",
    "get_captcha_verifier",
    "is_captcha_enabled",
    "verify_captcha",
]
