import hmac

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, TimestampSigner

from . import config

COOKIE = "cp_admin"
MAX_AGE = 7 * 24 * 3600
_signer = TimestampSigner(config.SECRET_KEY, salt="admin")


def check_password(password: str) -> bool:
    return bool(config.ADMIN_PASSWORD) and hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())


def session_token() -> str:
    return _signer.sign(b"admin").decode()


def is_admin(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        _signer.unsign(token, max_age=MAX_AGE)
        return True
    except BadSignature:
        return False


def require_admin(request: Request) -> None:
    """Dependency for admin routes: bounce to the login page."""
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": f"/admin/login?next={request.url.path}"})
