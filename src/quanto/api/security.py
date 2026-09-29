from __future__ import annotations

import hmac
import ipaddress
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse


def local_host(host: str) -> bool:
    try:
        value = (urlsplit(f"//{host}").hostname or "").lower()
    except ValueError:
        return False
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def session_token(root: Path) -> str:
    path = root / "session.token"
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="ascii") as stream:
                stream.write(value)
        except FileExistsError:
            return path.read_text(encoding="ascii").strip()
        return value


def security_error(request: Request, token: str):
    host = request.headers.get("host", "")
    if not local_host(host):
        return JSONResponse({"code": "INVALID_HOST", "message": "仅允许本机访问", "details": {}}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None:
        parsed = urlsplit(origin)
        expected = f"{request.url.scheme}://{host}".lower()
        if origin.lower() != expected or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return JSONResponse({"code": "INVALID_ORIGIN", "message": "请求来源不匹配", "details": {}}, status_code=403)
    if request.url.path == "/api/session" and request.method == "GET":
        return None
    if request.method in {"GET", "HEAD"} and not (request.url.path == "/api" or request.url.path.startswith("/api/")):
        return None
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {token}"):
        return JSONResponse({"code": "UNAUTHORIZED", "message": "访问令牌无效", "details": {}}, status_code=401)
    return None
