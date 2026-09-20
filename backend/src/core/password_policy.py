# Password Policy - 密码强度策略
"""阶段 5：把「只有长度」的密码校验换成现代口径。

依据 NIST SP 800-63B 的建议 —— **不做复杂度强制**。强制「大小写 + 数字 + 符号」
只会把用户推向 `Password1!` 这种可预测形态，实际熵增很小，却显著增加了记忆负担。
真正有效的是三件事：足够长度、挡住已知弱密码、不允许与身份信息重合。

四条规则：
1. 8–72 字节（上限来自 bcrypt 只取前 72 字节，超出部分会被静默忽略）；
2. 不在常见弱密码黑名单里；
3. 不与用户名 / 邮箱本地部分（>= 4 字符）重合；
4. 无 4 位以上的连续序列（`1234` / `abcd` / `qwer`）与 4 个以上重复字符。

**返回拒绝原因而非布尔值**：前端要能说清「为什么不行」，否则用户只能瞎猜着重试，
这本身就是把用户推向更弱的密码。
"""

from typing import Optional

from src.core.security import MAX_PASSWORD_BYTES

MIN_PASSWORD_BYTES = 8

# 身份片段的最小长度：短于 4 字符的片段（如用户名 "ab"）在密码里偶然出现太正常，
# 拿它判重合会大面积误伤
IDENTITY_MIN_FRAGMENT = 4

# 连续序列与重复字符的判定窗口
SEQUENCE_RUN = 4
REPEAT_RUN = 4

# 键盘行走序列（横向与斜向）：`qwer` / `asdf` 这类在泄露库里高频出现
_SEQUENCES = (
    "0123456789",
    "abcdefghijklmnopqrstuvwxyz",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "1qaz2wsx",
    "qazwsxedc",
)

# 常见弱密码黑名单（中英混合，取自公开泄露库的高频项）。
# 刻意剔除带侮辱性的词条：这份名单会被写进代码、可能出现在日志与文档里。
_COMMON_PASSWORDS = frozenset(
    {
        "123456", "password", "12345678", "qwerty", "123456789", "12345", "1234",
        "111111", "1234567", "123123", "abc123", "666666", "123321", "654321",
        "1234567890", "000000", "7777777", "121212", "112233", "555555", "131313",
        "777777", "888888", "222222", "987654321", "1234qwer", "11111111",
        "qwertyuiop", "zxcvbnm", "asdfgh", "qazwsx", "1qaz2wsx", "qwer1234",
        "1q2w3e4r", "1qazxsw2", "q1w2e3r4t5", "qaz123", "qweqwe", "asdfasdf",
        "0987654321", "987654", "789456", "123654", "456789", "11223344",
        "password1", "password123", "passw0rd", "p@ssw0rd", "letmein", "welcome",
        "welcome1", "admin", "admin123", "administrator", "root", "root123",
        "test", "test123", "guest", "master", "master123", "changeme", "default",
        "secret", "summer", "winter", "spring", "autumn", "monkey", "dragon",
        "baseball", "football", "soccer", "hockey", "batman", "superman",
        "starwars", "matrix", "phoenix", "falcon", "tigger", "sunshine",
        "iloveyou", "iloveu", "loveyou", "love", "angel", "shadow", "ninja",
        "samsung", "samsung123", "huawei", "xiaomi123", "apple123", "google123",
        "computer", "internet", "access", "gateway", "network", "system",
        "server", "database", "oracle", "mysql", "postgres", "sqlserver",
        "qwerty123", "abc123456", "123abc", "a123456", "a123456789", "aa123456",
        "123456a", "123456qq", "qq123456", "123456789a", "12345678910",
        "5201314", "woaini", "woaini1314", "woaini520", "1314520", "520520",
        "zxcvbnm123", "qqqqqq", "aaaaaa", "aaaaaaa", "aaaaaaaa", "zzzzzz",
        "xxxxxx", "abcdef", "abcabc", "abcd1234", "abcd123456", "qwertyui",
        "asdfghjkl", "zxcvb", "poiuyt", "lkjhgf", "mnbvcxz", "qwerty12345",
        "hello", "hello123", "hi123456", "freedom", "whatever", "trustno1",
        "mustang", "camaro", "corvette", "ferrari", "mercedes", "porsche",
        "yamaha", "harley", "toyota", "honda", "nissan", "guitar", "piano",
        "cookie", "orange", "banana", "pepper", "ginger", "purple", "yellow",
        "silver", "golden", "diamond", "magic", "flying", "running", "walker",
        "hunter", "killer", "sniper", "ranger", "soldier", "pilot", "captain",
        "doctor", "nurse", "teacher", "student", "school", "college", "university",
        "010203", "102030", "123321", "456123", "654123", "741852", "852963",
        "963852", "159357", "753951", "159753", "951753", "74108520",
    }
)


def byte_length(value: str) -> int:
    """按字节计算长度：bcrypt 的 72 字节上限是字节口径，不是字符口径"""
    return len(value.encode("utf-8"))


def _has_sequence(value: str) -> bool:
    """是否含长度 >= SEQUENCE_RUN 的连续序列（正序或倒序）"""
    lowered = value.lower()
    for sequence in _SEQUENCES:
        for start in range(len(sequence) - SEQUENCE_RUN + 1):
            fragment = sequence[start : start + SEQUENCE_RUN]
            if fragment in lowered or fragment[::-1] in lowered:
                return True
    return False


def _has_long_repeat(value: str) -> bool:
    """是否含长度 >= REPEAT_RUN 的同一字符重复（`aaaa` / `1111`）"""
    run = 1
    for index in range(1, len(value)):
        if value[index] == value[index - 1]:
            run += 1
            if run >= REPEAT_RUN:
                return True
        else:
            run = 1
    return False


def _identity_fragments(username: Optional[str], email: Optional[str]) -> list[str]:
    """从用户名与邮箱取出用于重合判定的片段（邮箱只取 @ 前的本地部分）"""
    fragments: list[str] = []
    for raw in (username, (email or "").split("@")[0]):
        candidate = (raw or "").strip().lower()
        if len(candidate) >= IDENTITY_MIN_FRAGMENT:
            fragments.append(candidate)
    return fragments


def check_password(
    password: str,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[str]:
    """校验密码强度。通过返回 None，否则返回面向用户的拒绝原因。

    调用方负责把它翻译成 422 业务错误（`WEAK_PASSWORD`）。
    """
    if byte_length(password) < MIN_PASSWORD_BYTES:
        return f"密码至少需要 {MIN_PASSWORD_BYTES} 个字符。"
    if byte_length(password) > MAX_PASSWORD_BYTES:
        return f"密码过长（最多 {MAX_PASSWORD_BYTES} 字节）。"

    lowered = password.lower()

    # 黑名单命中不区分大小写：`Password` 和 `password` 在攻击者眼里是同一个词
    if lowered in _COMMON_PASSWORDS:
        return "这个密码太常见了，容易被猜到，请换一个。"

    if _has_long_repeat(password):
        return f"密码不能包含连续 {REPEAT_RUN} 个以上相同字符。"

    if _has_sequence(password):
        return f"密码不能包含连续 {SEQUENCE_RUN} 位以上的顺序字符（如 1234、abcd）。"

    for fragment in _identity_fragments(username, email):
        if fragment in lowered:
            return "密码不能包含用户名或邮箱中的连续片段。"

    return None


__all__ = ["MIN_PASSWORD_BYTES", "byte_length", "check_password"]
