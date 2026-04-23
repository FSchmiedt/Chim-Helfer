"""Auth: Admin-Session und Helfer-Session über signierte Cookies.

Zwei getrennte Cookies, damit ein Admin-Login nicht eine Helfer-Session 'hijacken'
kann und umgekehrt. Der Admin bleibt passwortbasiert über ENV; Helfer:innen
authentifizieren sich mit ihrer Email + Passwort (siehe `passwords.py`).
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from . import models
from .config import settings

_admin_serializer = URLSafeSerializer(settings.SECRET_KEY, salt="chimaera-admin-session")
_helper_serializer = URLSafeSerializer(settings.SECRET_KEY, salt="chimaera-helper-session")

ADMIN_COOKIE_NAME = "chimaera_session"
HELPER_COOKIE_NAME = "chimaera_helper_session"

# Rückwärtskompatibilität: wer woanders noch `COOKIE_NAME` importiert
COOKIE_NAME = ADMIN_COOKIE_NAME


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
def check_credentials(username: str, password: str) -> bool:
    """Konstantzeit-Vergleich für Admin-Credentials aus ENV."""
    u_ok = secrets.compare_digest(username or "", settings.ADMIN_USERNAME)
    p_ok = secrets.compare_digest(password or "", settings.ADMIN_PASSWORD)
    return u_ok and p_ok


def make_session_cookie() -> str:
    return _admin_serializer.dumps({"admin": True})


def is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return False
    try:
        data = _admin_serializer.loads(token)
        return bool(data.get("admin"))
    except BadSignature:
        return False


def require_admin(request: Request):
    """FastAPI-Dependency für JSON-Endpoints."""
    if not is_admin(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin-Login erforderlich")
    return True


def require_admin_redirect(request: Request) -> Optional[RedirectResponse]:
    """Für HTML-Seiten: Redirect zurück zum Login, wenn nicht eingeloggt."""
    if not is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    return None


# ---------------------------------------------------------------------------
# Helfer:in
# ---------------------------------------------------------------------------
def make_helper_session_cookie(helper_id: int) -> str:
    return _helper_serializer.dumps({"helper_id": int(helper_id)})


def get_current_helper_id(request: Request) -> Optional[int]:
    token = request.cookies.get(HELPER_COOKIE_NAME)
    if not token:
        return None
    try:
        data = _helper_serializer.loads(token)
        hid = data.get("helper_id")
        return int(hid) if hid is not None else None
    except (BadSignature, ValueError, TypeError):
        return None


def get_current_helper(request: Request, db: Session) -> Optional[models.Helper]:
    hid = get_current_helper_id(request)
    if hid is None:
        return None
    return db.get(models.Helper, hid)


def require_helper_redirect(request: Request, db: Session) -> tuple[Optional[RedirectResponse], Optional[models.Helper]]:
    """Für /me-Seiten: entweder (Redirect, None) oder (None, Helper-Objekt)."""
    helper = get_current_helper(request, db)
    if not helper:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER), None
    return None, helper
