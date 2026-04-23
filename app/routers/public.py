"""Öffentliche Routen: Anmeldung, Helfer-Login, Passwort-Reset."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, ValidationError, field_validator
from sqlalchemy.orm import Session

from .. import models
from ..auth import HELPER_COOKIE_NAME, make_helper_session_cookie
from ..config import settings
from ..database import get_db
from ..passwords import generate_token, hash_password, verify_password


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

RESET_TOKEN_VALIDITY = timedelta(hours=2)
PASSWORD_MIN_LEN = 8


# ---------------------------------------------------------------------------
# Registrierung
# ---------------------------------------------------------------------------
class RegistrationInput(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone: str | None = None
    date_of_birth: date
    iban: str | None = None
    paypal: str | None = None
    been_here_before: bool = False
    previous_festivals: str | None = None
    notes: str | None = None
    availability_day_ids: list[int]
    area_preferences: dict[int, int]  # area_id -> rank
    is_adult_confirmed: bool
    accepted_no_guarantee: bool
    password: str
    password_confirm: str

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_and_check(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Pflichtfeld")
        return v

    @field_validator("is_adult_confirmed")
    @classmethod
    def must_be_adult(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Du musst volljährig sein.")
        return v

    @field_validator("accepted_no_guarantee")
    @classmethod
    def must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Bitte bestätige die Hinweise.")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def must_be_18(cls, v: date) -> date:
        today = date.today()
        age = today.year - v.year - ((today.month, today.day) < (v.month, v.day))
        if age < 18:
            raise ValueError("Du musst mindestens 18 Jahre alt sein.")
        if age > 100:
            raise ValueError("Geburtsdatum scheint ungültig.")
        return v

    @field_validator("availability_day_ids")
    @classmethod
    def at_least_one_day(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("Bitte wähle mindestens einen Tag aus.")
        return v

    @field_validator("password")
    @classmethod
    def password_strong_enough(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LEN:
            raise ValueError(f"Passwort muss mindestens {PASSWORD_MIN_LEN} Zeichen lang sein.")
        return v


# ---------------------------------------------------------------------------
# Formular anzeigen
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def register_form(request: Request, db: Session = Depends(get_db)):
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "days": days,
            "areas": areas,
            "min_shifts": settings.MIN_SHIFTS,
            "min_days": settings.MIN_DAYS,
            "benefits_min_shifts": settings.BENEFITS_MIN_SHIFTS,
            "benefits_min_days": settings.BENEFITS_MIN_DAYS,
            "password_min_len": PASSWORD_MIN_LEN,
            "errors": None,
            "form_data": None,
        },
    )


# ---------------------------------------------------------------------------
# Formular absenden
# ---------------------------------------------------------------------------
@router.post("/register", response_class=HTMLResponse)
async def register_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    # Listen + Dicts aus Form-Daten extrahieren
    availability_day_ids = [int(x) for x in form.getlist("availability_day_ids")]
    area_preferences: dict[int, int] = {}
    for key in form:
        if key.startswith("area_rank_"):
            try:
                area_id = int(key.split("_")[-1])
                rank_str = form.get(key)
                if rank_str and rank_str != "":
                    rank = int(rank_str)
                    if rank >= 1:
                        area_preferences[area_id] = rank
            except ValueError:
                continue

    # Pydantic-Validierung
    raw = {
        "first_name": form.get("first_name", ""),
        "last_name": form.get("last_name", ""),
        "email": form.get("email", "").strip().lower(),
        "phone": (form.get("phone") or "").strip() or None,
        "date_of_birth": form.get("date_of_birth", ""),
        "iban": (form.get("iban") or "").strip().replace(" ", "") or None,
        "paypal": (form.get("paypal") or "").strip() or None,
        "been_here_before": form.get("been_here_before") == "yes",
        "previous_festivals": (form.get("previous_festivals") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
        "availability_day_ids": availability_day_ids,
        "area_preferences": area_preferences,
        "is_adult_confirmed": form.get("is_adult_confirmed") == "on",
        "accepted_no_guarantee": form.get("accepted_no_guarantee") == "on",
        "password": form.get("password") or "",
        "password_confirm": form.get("password_confirm") or "",
    }

    # Passwort-Wiederholung zuerst prüfen (sonst verschlucken wir den Fehler,
    # falls password schon an anderer Regel scheitert).
    if raw["password"] != raw["password_confirm"]:
        return _render_form_with_errors(
            request, db,
            {"password_confirm": "Passwörter stimmen nicht überein."},
            raw,
        )

    try:
        data = RegistrationInput(**raw)
    except ValidationError as exc:
        errors = {err["loc"][0]: err["msg"] for err in exc.errors()}
        return _render_form_with_errors(request, db, errors, raw)

    # Mindestens ein Bereich gewählt?
    if not area_preferences:
        return _render_form_with_errors(
            request, db,
            {"area_preferences": "Bitte wähle mindestens einen Wunschbereich."},
            raw,
        )

    # Doppel-Anmeldung?
    existing = db.query(models.Helper).filter(models.Helper.email == data.email).one_or_none()
    if existing:
        return _render_form_with_errors(
            request, db,
            {"email": "Eine Anmeldung mit dieser Email existiert bereits. Falls du dein Passwort vergessen hast, nutze 'Passwort zurücksetzen'."},
            raw,
        )

    # Helper anlegen
    helper = models.Helper(
        first_name=data.first_name,
        last_name=data.last_name,
        email=data.email,
        phone=data.phone,
        date_of_birth=data.date_of_birth,
        iban=data.iban,
        paypal=data.paypal,
        been_here_before=data.been_here_before,
        previous_festivals=data.previous_festivals if data.been_here_before else None,
        notes=data.notes,
        is_adult_confirmed=data.is_adult_confirmed,
        accepted_no_guarantee=data.accepted_no_guarantee,
        status="registered",
        password_hash=hash_password(data.password),
    )
    db.add(helper)
    db.flush()

    # Verfügbarkeiten
    valid_day_ids = {d.id for d in db.query(models.FestivalDay).all()}
    for day_id in data.availability_day_ids:
        if day_id in valid_day_ids:
            db.add(models.Availability(helper_id=helper.id, day_id=day_id))

    # Wunschbereiche
    valid_area_ids = {a.id for a in db.query(models.Area).all()}
    for area_id, rank in data.area_preferences.items():
        if area_id in valid_area_ids:
            db.add(models.HelperAreaPreference(helper_id=helper.id, area_id=area_id, rank=rank))

    db.commit()

    # Erfolg + automatisch einloggen
    resp = templates.TemplateResponse(
        "register_success.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "helper": helper,
        },
    )
    resp.set_cookie(
        HELPER_COOKIE_NAME,
        make_helper_session_cookie(helper.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 Tage
        secure=False,
    )
    return resp


def _render_form_with_errors(request: Request, db: Session, errors: dict, raw: dict):
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "days": days,
            "areas": areas,
            "min_shifts": settings.MIN_SHIFTS,
            "min_days": settings.MIN_DAYS,
            "benefits_min_shifts": settings.BENEFITS_MIN_SHIFTS,
            "benefits_min_days": settings.BENEFITS_MIN_DAYS,
            "password_min_len": PASSWORD_MIN_LEN,
            "errors": errors,
            "form_data": raw,
        },
        status_code=status.HTTP_400_BAD_REQUEST,
    )


# ---------------------------------------------------------------------------
# Helfer-Login / Logout
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def helper_login_form(request: Request):
    return templates.TemplateResponse(
        "helper_login.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "error": None,
            "email": "",
            "next": request.query_params.get("next", "/me"),
        },
    )


@router.post("/login")
async def helper_login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    next_url = form.get("next") or "/me"
    # Niemals auf externe URLs redirecten
    if not next_url.startswith("/"):
        next_url = "/me"

    helper = db.query(models.Helper).filter(models.Helper.email == email).one_or_none()
    if not helper or not verify_password(password, helper.password_hash):
        return templates.TemplateResponse(
            "helper_login.html",
            {
                "request": request,
                "festival_name": settings.FESTIVAL_NAME,
                "error": "Email oder Passwort stimmt nicht. Noch kein Passwort? Nutze 'Passwort zurücksetzen'.",
                "email": email,
                "next": next_url,
            },
            status_code=401,
        )

    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(
        HELPER_COOKIE_NAME,
        make_helper_session_cookie(helper.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        secure=False,
    )
    return resp


@router.post("/logout")
def helper_logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(HELPER_COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# Passwort vergessen
# ---------------------------------------------------------------------------
@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(
        "helper_forgot.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "submitted": False,
            "manual_link": None,
        },
    )


@router.post("/forgot", response_class=HTMLResponse)
async def forgot_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()

    helper = db.query(models.Helper).filter(models.Helper.email == email).one_or_none()

    manual_link: Optional[str] = None
    if helper:
        token = generate_token()
        helper.password_reset_token = token
        helper.password_reset_expires = datetime.utcnow() + RESET_TOKEN_VALIDITY
        db.commit()

        reset_url = f"{request.base_url}reset/{token}".replace("http://", "http://").rstrip("/")
        # base_url endet auf "/", aber nach dem Replace kann Müll drin sein. Sauber:
        base = str(request.base_url).rstrip("/")
        reset_url = f"{base}/reset/{token}"

        # Versuche SMTP-Versand. Klappt das nicht, zeigen wir dem Admin eine
        # Info-Seite (für Self-Hosted ohne SMTP) – nicht dem Nutzer, um
        # Email-Enumeration zu vermeiden.
        if settings.smtp_enabled:
            try:
                from ..email_sender import send_password_reset_email
                send_password_reset_email(helper, reset_url)
            except Exception as exc:  # noqa: BLE001
                # SMTP down → lieber auffällig loggen, als den Nutzer scheitern zu lassen
                print(f"[forgot] SMTP-Fehler: {exc}")
        else:
            # Kein SMTP konfiguriert: wir printen den Link in die Server-Konsole,
            # damit der Admin ihn notfalls weiterleiten kann.
            print(f"[forgot] Reset-Link für {email}: {reset_url}")
            # In Dev darf die Seite den Link zeigen. In Prod lieber nicht –
            # deshalb nur wenn DEBUG_SHOW_RESET_LINK=true gesetzt ist.
            if getattr(settings, "DEBUG_SHOW_RESET_LINK", False):
                manual_link = reset_url

    # Auf jeden Fall die gleiche Erfolgsseite zeigen (keine Enumeration).
    return templates.TemplateResponse(
        "helper_forgot.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "submitted": True,
            "manual_link": manual_link,
        },
    )


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    helper = _find_helper_by_reset_token(db, token)
    return templates.TemplateResponse(
        "helper_reset.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "token": token,
            "valid": helper is not None,
            "password_min_len": PASSWORD_MIN_LEN,
            "error": None,
        },
    )


@router.post("/reset/{token}", response_class=HTMLResponse)
async def reset_submit(token: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    password = form.get("password") or ""
    password_confirm = form.get("password_confirm") or ""

    helper = _find_helper_by_reset_token(db, token)
    if not helper:
        return templates.TemplateResponse(
            "helper_reset.html",
            {
                "request": request,
                "festival_name": settings.FESTIVAL_NAME,
                "token": token,
                "valid": False,
                "password_min_len": PASSWORD_MIN_LEN,
                "error": None,
            },
            status_code=400,
        )

    error: Optional[str] = None
    if len(password) < PASSWORD_MIN_LEN:
        error = f"Passwort muss mindestens {PASSWORD_MIN_LEN} Zeichen lang sein."
    elif password != password_confirm:
        error = "Passwörter stimmen nicht überein."

    if error:
        return templates.TemplateResponse(
            "helper_reset.html",
            {
                "request": request,
                "festival_name": settings.FESTIVAL_NAME,
                "token": token,
                "valid": True,
                "password_min_len": PASSWORD_MIN_LEN,
                "error": error,
            },
            status_code=400,
        )

    helper.password_hash = hash_password(password)
    helper.password_reset_token = None
    helper.password_reset_expires = None
    db.commit()

    # Direkt einloggen
    resp = RedirectResponse("/me", status_code=303)
    resp.set_cookie(
        HELPER_COOKIE_NAME,
        make_helper_session_cookie(helper.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        secure=False,
    )
    return resp


def _find_helper_by_reset_token(db: Session, token: str) -> Optional[models.Helper]:
    if not token:
        return None
    helper = db.query(models.Helper).filter(models.Helper.password_reset_token == token).one_or_none()
    if not helper:
        return None
    if not helper.password_reset_expires or helper.password_reset_expires < datetime.utcnow():
        return None
    return helper
