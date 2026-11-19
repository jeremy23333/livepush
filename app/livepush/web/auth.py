"""极简会话鉴权：口令比对 + HMAC 签名 Cookie。

个人自用、默认只监听 127.0.0.1，所以不引入完整用户体系；
但推送能发到群里，所以仍然要求登录，避免误暴露到局域网后被别人操作。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status

from ..config import runtime

COOKIE_NAME = "lp_session"
SESSION_TTL = 7 * 86400


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def verify_password(password: str) -> bool:
    expected = runtime.admin_password or ""
    if not expected:
        return False
    return hmac.compare_digest(password.encode(), expected.encode())


def issue_session(username: str = "admin") -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = f"{username}|{expires}".encode()
    signature = hmac.new(runtime.secret_key.encode(), payload, hashlib.sha256).hexdigest()
    return f"{_b64(payload)}.{signature}"


def validate_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = _unb64(encoded)
        expires = int(payload.decode().split("|")[-1])
    except (ValueError, UnicodeDecodeError):
        return False
    if expires < int(time.time()):
        return False
    expected = hmac.new(runtime.secret_key.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def is_authenticated(request: Request) -> bool:
    return validate_session(request.cookies.get(COOKIE_NAME))


def require_user(request: Request) -> str:
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return "admin"


def require_csrf(request: Request) -> None:
    """状态变更接口必须带自定义头。

    浏览器对跨站请求会先做预检，而我们不返回 CORS 允许头，
    所以带自定义头 + SameSite=Lax 的组合足以挡住 CSRF。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get("x-lp") != "1":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少防跨站请求头")
