"""情景记忆边界脱敏：在 embedding、持久化及检索返回前执行。"""
import re

_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ID = re.compile(r"(?<!\d)(?:\d{17}[0-9Xx]|\d{15})(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


def redact(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    text = _EMAIL.sub("[邮箱]", text)
    text = _ID.sub("[身份证]", text)
    return _PHONE.sub("[手机号]", text)
