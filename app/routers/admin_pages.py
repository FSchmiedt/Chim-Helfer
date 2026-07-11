"""Admin-HTML-Seiten: Login, Dashboard, Helferübersicht, Schichten, Mail."""
from __future__ import annotations

import io
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile, File, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..auth import (
    COOKIE_NAME,
    check_credentials,
    is_admin,
    make_session_cookie,
    require_admin_redirect,
)
from ..config import settings
from ..csv_io import emails_to_csv, helpers_to_csv, import_helpers_from_csv
from ..database import get_db


router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _parse_int_or_none(value: str | None) -> int | None:
    """Filter-Selects senden '' für 'alle'. Leer + ungültig → None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _ctx(request: Request, **extra) -> dict:
    ctx = {"request": request, "festival_name": settings.FESTIVAL_NAME, "smtp_enabled": settings.smtp_enabled}
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("admin_login.html", _ctx(request, error=None))


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not check_credentials(username, password):
        return templates.TemplateResponse(
            "admin_login.html",
            _ctx(request, error="Falsche Zugangsdaten."),
            status_code=401,
        )
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        make_session_cookie(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 8,  # 8 Stunden
        secure=False,  # in Produktion via Reverse Proxy auf True setzen
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r

    total_helpers = db.query(func.count(models.Helper.id)).scalar() or 0
    status_counts: dict[str, int] = dict(
        db.query(models.Helper.status, func.count(models.Helper.id)).group_by(models.Helper.status).all()
    )
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()

    # Helfer pro Tag
    day_counts = dict(
        db.query(models.Availability.day_id, func.count(models.Availability.helper_id))
        .group_by(models.Availability.day_id).all()
    )
    # Wunschbereiche-Verteilung (nur erste Wahl)
    area_first_counts = dict(
        db.query(models.HelperAreaPreference.area_id, func.count(models.HelperAreaPreference.id))
        .filter(models.HelperAreaPreference.rank == 1)
        .group_by(models.HelperAreaPreference.area_id).all()
    )

    # Schichten
    total_shifts = db.query(func.count(models.Shift.id)).scalar() or 0
    total_shift_capacity = db.query(func.coalesce(func.sum(models.Shift.capacity), 0)).scalar() or 0
    total_assigned = db.query(func.count(models.ShiftAssignment.id)).scalar() or 0
    open_slots = max(0, total_shift_capacity - total_assigned)

    # Nicht verplante Helfer:innen
    assigned_helper_ids = db.query(models.ShiftAssignment.helper_id).distinct().all()
    assigned_helper_ids = {r[0] for r in assigned_helper_ids}
    all_helper_ids = {r[0] for r in db.query(models.Helper.id).all()}
    unassigned_count = len(all_helper_ids - assigned_helper_ids)

    # Pfand-Übersicht
    pfand_paid_count = db.query(func.count(models.Helper.id)).filter(
        models.Helper.pfand_paid.is_(True), models.Helper.pfand_returned.is_(False)
    ).scalar() or 0
    pfand_returned_count = db.query(func.count(models.Helper.id)).filter(
        models.Helper.pfand_returned.is_(True)
    ).scalar() or 0

    return templates.TemplateResponse(
        "admin/dashboard.html",
        _ctx(
            request,
            total_helpers=total_helpers,
            status_counts=status_counts,
            days=days,
            day_counts=day_counts,
            areas=areas,
            area_first_counts=area_first_counts,
            total_shifts=total_shifts,
            total_capacity=total_shift_capacity,
            total_assigned=total_assigned,
            open_slots=open_slots,
            unassigned_count=unassigned_count,
            pfand_paid_count=pfand_paid_count,
            pfand_returned_count=pfand_returned_count,
        ),
    )


# ---------------------------------------------------------------------------
# Helfer:in manuell hinzufügen (z.B. Walk-in, Telefonanmeldung)
# ---------------------------------------------------------------------------
@router.get("/helpers/new", response_class=HTMLResponse)
def helper_new_form(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    days = db.query(models.FestivalDay).order_by(
        models.FestivalDay.sort_order, models.FestivalDay.date
    ).all()
    areas = db.query(models.Area).order_by(
        models.Area.sort_order, models.Area.name
    ).all()
    return templates.TemplateResponse(
        "admin/helper_new.html",
        _ctx(request, days=days, areas=areas, errors=None, form_data=None),
    )


@router.post("/helpers/new", response_class=HTMLResponse)
async def helper_new_submit(request: Request, db: Session = Depends(get_db)):
    """Pragmatischer Pflicht-Datensatz: Vorname, Nachname, Email.
    Optional: Telefon, Geburtsdatum, Tage, Wunschbereiche, Admin-Notiz."""
    from datetime import date as ddate, datetime as ddatetime
    if (r := require_admin_redirect(request)):
        return r

    form = await request.form()
    errors: dict[str, str] = {}

    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    phone = (form.get("phone") or "").strip() or None
    dob_raw = (form.get("date_of_birth") or "").strip()
    admin_notes = (form.get("admin_notes") or "").strip() or None
    availability_day_ids = [int(x) for x in form.getlist("availability_day_ids") if x]

    # Wunschbereiche – wie im öffentlichen Formular: alle Bereiche bekommen
    # einen Rang. Leer = Prio 5.
    all_areas = db.query(models.Area).all()
    area_preferences: dict[int, int] = {}
    for a in all_areas:
        raw = (form.get(f"area_rank_{a.id}") or "").strip()
        if not raw:
            area_preferences[a.id] = 5
            continue
        try:
            r_val = int(raw)
            area_preferences[a.id] = r_val if 1 <= r_val <= 5 else 5
        except ValueError:
            area_preferences[a.id] = 5

    # Validierung
    if not first_name:
        errors["first_name"] = "Pflichtfeld"
    if not last_name:
        errors["last_name"] = "Pflichtfeld"
    if not email or "@" not in email:
        errors["email"] = "Gültige Email-Adresse nötig"
    elif db.query(models.Helper).filter(models.Helper.email == email).one_or_none():
        errors["email"] = "Email existiert bereits"

    dob_parsed: ddate | None = None
    if dob_raw:
        try:
            dob_parsed = ddate.fromisoformat(dob_raw)
        except ValueError:
            errors["date_of_birth"] = "Ungültiges Datum"

    if errors:
        days = db.query(models.FestivalDay).order_by(
            models.FestivalDay.sort_order, models.FestivalDay.date
        ).all()
        return templates.TemplateResponse(
            "admin/helper_new.html",
            _ctx(request, days=days, areas=all_areas,
                 errors=errors, form_data=dict(form)),
            status_code=400,
        )

    # Zugang: optionales Passwort + Verifikations-Mail-Verhalten
    password_raw = (form.get("password") or "").strip()
    send_verify = form.get("send_verify_email") == "on"

    if password_raw and len(password_raw) < 8:
        errors["password"] = "Mindestens 8 Zeichen, oder leer lassen."

    if errors:
        days = db.query(models.FestivalDay).order_by(
            models.FestivalDay.sort_order, models.FestivalDay.date
        ).all()
        return templates.TemplateResponse(
            "admin/helper_new.html",
            _ctx(request, days=days, areas=all_areas,
                 errors=errors, form_data=dict(form)),
            status_code=400,
        )

    # Helper-Objekt aufbauen. Geburtsdatum default 1990-01-01 (Spalte ist NOT NULL).
    from ..passwords import hash_password, generate_token
    helper_kwargs = dict(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        date_of_birth=dob_parsed or ddate(1990, 1, 1),
        is_adult_confirmed=True,  # Admin bürgt
        accepted_no_guarantee=True,
        status="registered",
        admin_notes=admin_notes,
    )

    if password_raw:
        helper_kwargs["password_hash"] = hash_password(password_raw)

    if send_verify:
        # Email noch nicht verifiziert; Token wird gleich erzeugt und gemailt.
        helper_kwargs["email_verification_token"] = generate_token()
    else:
        # Email gilt sofort als verifiziert.
        helper_kwargs["email_verified_at"] = ddatetime.utcnow()

    helper = models.Helper(**helper_kwargs)
    db.add(helper)
    db.flush()

    valid_day_ids = {d.id for d in db.query(models.FestivalDay).all()}
    for did in availability_day_ids:
        if did in valid_day_ids:
            db.add(models.Availability(helper_id=helper.id, day_id=did))

    valid_area_ids = {a.id for a in all_areas}
    for aid, rank in area_preferences.items():
        if aid in valid_area_ids:
            db.add(models.HelperAreaPreference(
                helper_id=helper.id, area_id=aid, rank=rank,
            ))

    db.commit()

    # Verifikations-Mail rausschicken, falls angefordert
    if send_verify:
        base = str(request.base_url).rstrip("/")
        verify_url = f"{base}/verify/{helper.email_verification_token}"
        cc_address = settings.SMTP_FROM_ADDRESS or None
        if settings.smtp_enabled:
            try:
                from ..email_sender import send_verification_email
                send_verification_email(helper, verify_url, cc=cc_address)
            except Exception as exc:  # noqa: BLE001
                print(f"[admin helper_new] SMTP-Fehler beim Verify-Versand: {exc}")
        else:
            print(f"[admin helper_new] SMTP nicht konfiguriert. "
                  f"Verifikations-Link für {helper.email}: {verify_url}")

    return RedirectResponse(f"/admin/helpers/{helper.id}", status_code=303)


# ---------------------------------------------------------------------------
# Helferübersicht
# ---------------------------------------------------------------------------
@router.get("/helpers", response_class=HTMLResponse)
def helpers_list(
    request: Request,
    db: Session = Depends(get_db),
    day_id: str | None = Query(None),
    area_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    experience: str | None = Query(None),  # "yes" | "no"
    pfand: str | None = Query(None),  # "unpaid" | "paid" | "returned"
    verified: str | None = Query(None),  # "yes" | "no"
    q: str | None = Query(None),
):
    if (r := require_admin_redirect(request)):
        return r

    day_id_int = _parse_int_or_none(day_id)
    area_id_int = _parse_int_or_none(area_id)

    query = db.query(models.Helper).options(
        joinedload(models.Helper.availabilities).joinedload(models.Availability.day),
        joinedload(models.Helper.preferences).joinedload(models.HelperAreaPreference.area),
    )
    if day_id_int:
        query = query.join(models.Availability).filter(models.Availability.day_id == day_id_int)
    if area_id_int:
        # Prio 5 ist der Default "egal" und bedeutet kein aktives
        # Interesse am Bereich. Beim Filtern zeigen wir nur Helfer:innen
        # mit Prio 1-4 für diesen Bereich.
        query = query.join(models.HelperAreaPreference).filter(
            models.HelperAreaPreference.area_id == area_id_int,
            models.HelperAreaPreference.rank < 5,
        )
    if status_filter:
        query = query.filter(models.Helper.status == status_filter)
    if experience == "yes":
        query = query.filter(models.Helper.been_here_before.is_(True))
    elif experience == "no":
        query = query.filter(models.Helper.been_here_before.is_(False))
    if pfand == "unpaid":
        query = query.filter(models.Helper.pfand_paid.is_(False))
    elif pfand == "paid":
        query = query.filter(models.Helper.pfand_paid.is_(True), models.Helper.pfand_returned.is_(False))
    elif pfand == "returned":
        query = query.filter(models.Helper.pfand_returned.is_(True))
    if verified == "yes":
        query = query.filter(models.Helper.email_verified_at.isnot(None))
    elif verified == "no":
        query = query.filter(models.Helper.email_verified_at.is_(None))
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            func.lower(models.Helper.first_name).like(like)
            | func.lower(models.Helper.last_name).like(like)
            | func.lower(models.Helper.email).like(like)
        )
    helpers = query.order_by(models.Helper.created_at.desc()).distinct().all()

    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()

    return templates.TemplateResponse(
        "admin/helpers.html",
        _ctx(
            request,
            helpers=helpers,
            days=days,
            areas=areas,
            day_id=day_id_int,
            area_id=area_id_int,
            status_filter=status_filter,
            experience=experience,
            pfand=pfand,
            verified=verified,
            q=q or "",
        ),
    )


@router.get("/helpers/{helper_id}", response_class=HTMLResponse)
def helper_detail(helper_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r

    helper = db.get(models.Helper, helper_id)
    if not helper:
        return HTMLResponse("Nicht gefunden", status_code=404)

    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()
    all_roles = db.query(models.Role).join(models.Area).order_by(models.Area.sort_order, models.Role.sort_order).all()

    trust_ids = {t.role_id for t in helper.role_trusts}
    pref_areas = {p.area_id for p in helper.preferences}

    # Rollen pro Bereich pre-gruppieren — damit fällt der
    # `{% if grouped_roles.update(...) %}`-Hack im Template weg.
    grouped_roles: dict[str, list[models.Role]] = {}
    area_by_name: dict[str, models.Area] = {}
    for r in all_roles:
        grouped_roles.setdefault(r.area.name, []).append(r)
        area_by_name.setdefault(r.area.name, r.area)

    assignments = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.helper_id == helper_id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
            joinedload(models.ShiftAssignment.role),
        )
        .all()
    )

    return templates.TemplateResponse(
        "admin/helper_detail.html",
        _ctx(
            request,
            helper=helper,
            days=days,
            areas=areas,
            all_roles=all_roles,
            grouped_roles=grouped_roles,
            area_by_name=area_by_name,
            trust_ids=trust_ids,
            pref_areas=pref_areas,
            assignments=assignments,
        ),
    )


async def _handle_helper_update(helper_id: int, request: Request, db: Session):
    """Ein Endpoint für alle Admin-Edits an einer Helfer:in.

    Welche Felder in einem bestimmten Submit geändert werden, steuert das
    Template durch die `section`-Hidden-Inputs. So können einzelne Panels
    (Stammdaten / Pfand / Zuweisung / Rollen) unabhängig gespeichert werden,
    ohne dass man aus Versehen ein leeres Feld woanders überschreibt.
    """
    from datetime import date as ddate, datetime as ddatetime

    helper = db.get(models.Helper, helper_id)
    if not helper:
        return HTMLResponse("Nicht gefunden", status_code=404)
    form = await request.form()
    sections = set(form.getlist("section"))

    # Default: wenn kein section-Hint kommt (z.B. alter Client), alles speichern
    if not sections:
        sections = {"admin", "contact", "prefs", "pfand"}

    # --- Admin-Meta (Status, interne Notizen, Rollen-Zutrauen) ---
    if "admin" in sections:
        helper.status = form.get("status", helper.status)
        helper.admin_notes = (form.get("admin_notes") or "").strip() or None
        new_role_ids = {int(x) for x in form.getlist("trusted_role_ids") if x}
        db.query(models.HelperRoleTrust).filter(
            models.HelperRoleTrust.helper_id == helper.id
        ).delete()
        for rid in new_role_ids:
            db.add(models.HelperRoleTrust(helper_id=helper.id, role_id=rid))

    # --- Stammdaten (Name, Kontakt, Geburtsdatum, IBAN, PayPal) ---
    if "contact" in sections:
        helper.first_name = (form.get("first_name") or helper.first_name).strip()
        helper.last_name = (form.get("last_name") or helper.last_name).strip()
        new_email = (form.get("email") or helper.email).strip().lower()
        if new_email and new_email != helper.email:
            # Unique-Prüfung
            existing = db.query(models.Helper).filter(
                models.Helper.email == new_email,
                models.Helper.id != helper.id,
            ).one_or_none()
            if not existing:
                helper.email = new_email
        helper.phone = (form.get("phone") or "").strip() or None
        dob_raw = (form.get("date_of_birth") or "").strip()
        if dob_raw:
            try:
                helper.date_of_birth = ddate.fromisoformat(dob_raw)
            except ValueError:
                pass
        helper.iban = (form.get("iban") or "").strip().replace(" ", "") or None
        helper.paypal = (form.get("paypal") or "").strip() or None
        helper.notes = (form.get("notes") or "").strip() or None

    # --- Verfügbarkeit + Wunschbereiche ---
    if "prefs" in sections:
        # Verfügbarkeit
        new_day_ids = {int(x) for x in form.getlist("availability_day_ids") if x}
        valid_day_ids = {d.id for d in db.query(models.FestivalDay).all()}
        new_day_ids &= valid_day_ids
        db.query(models.Availability).filter(
            models.Availability.helper_id == helper.id
        ).delete()
        for did in new_day_ids:
            db.add(models.Availability(helper_id=helper.id, day_id=did))

        # Wunschbereiche mit Rang
        valid_area_ids = {a.id for a in db.query(models.Area).all()}
        new_prefs: dict[int, int] = {}
        for key in form:
            if not key.startswith("area_rank_"):
                continue
            try:
                aid = int(key.split("_")[-1])
            except ValueError:
                continue
            if aid not in valid_area_ids:
                continue
            rank_raw = (form.get(key) or "").strip()
            if not rank_raw:
                continue
            try:
                rank = int(rank_raw)
            except ValueError:
                continue
            if rank >= 1:
                new_prefs[aid] = rank
        db.query(models.HelperAreaPreference).filter(
            models.HelperAreaPreference.helper_id == helper.id
        ).delete()
        for aid, rank in new_prefs.items():
            db.add(models.HelperAreaPreference(helper_id=helper.id, area_id=aid, rank=rank))

    # --- Pfand ---
    if "pfand" in sections:
        new_paid = form.get("pfand_paid") == "on"
        new_returned = form.get("pfand_returned") == "on"
        now = ddatetime.utcnow()
        # Erst Kante prüfen, dann setzen, damit Timestamps nur beim echten
        # Wechsel aktualisiert werden.
        if new_paid and not helper.pfand_paid:
            helper.pfand_paid_at = now
        if not new_paid:
            helper.pfand_paid_at = None
            # Wenn nicht mehr bezahlt, auch nicht zurückgegeben.
            new_returned = False
        helper.pfand_paid = new_paid

        if new_returned and not helper.pfand_returned:
            helper.pfand_returned_at = now
        if not new_returned:
            helper.pfand_returned_at = None
        helper.pfand_returned = new_returned

    db.commit()
    return RedirectResponse(f"/admin/helpers/{helper.id}", status_code=303)


@router.post("/helpers/{helper_id}/save")
async def helper_save(helper_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    return await _handle_helper_update(helper_id, request, db)


@router.post("/helpers/{helper_id}/delete")
def helper_delete(helper_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    helper = db.get(models.Helper, helper_id)
    if helper:
        db.delete(helper)
        db.commit()
    return RedirectResponse("/admin/helpers", status_code=303)


@router.post("/helpers/{helper_id}/reset-link", response_class=HTMLResponse)
def helper_reset_link(helper_id: int, request: Request, db: Session = Depends(get_db)):
    """Admin erzeugt einen Passwort-Reset-Link manuell (z.B. wenn SMTP nicht läuft
    oder Helfer:in ihre Email nicht mehr checkt). Der Link wird einmalig angezeigt."""
    from datetime import datetime, timedelta
    from ..passwords import generate_token

    if (r := require_admin_redirect(request)):
        return r
    helper = db.get(models.Helper, helper_id)
    if not helper:
        return HTMLResponse("Nicht gefunden", status_code=404)

    token = generate_token()
    helper.password_reset_token = token
    helper.password_reset_expires = datetime.utcnow() + timedelta(hours=24)
    db.commit()

    base = str(request.base_url).rstrip("/")
    reset_url = f"{base}/reset/{token}"

    # Wir laden die Detail-Seite mit dem frischen Link im Flash.
    return _redirect_to_helper_detail_with_link(request, db, helper, reset_url)


@router.post("/helpers/{helper_id}/resend-verify", response_class=HTMLResponse)
def helper_resend_verify(helper_id: int, request: Request, db: Session = Depends(get_db)):
    """Admin schickt die Email-Verifikations-Mail erneut. Eine Kopie geht an die
    konfigurierte Absender-Adresse (helfen@...), damit das Helfer-Team mitliest."""
    from datetime import datetime
    from ..passwords import generate_token

    if (r := require_admin_redirect(request)):
        return r
    helper = db.get(models.Helper, helper_id)
    if not helper:
        return HTMLResponse("Nicht gefunden", status_code=404)

    if helper.email_verified_at is not None:
        # Schon verifiziert — kein neuer Token, aber freundlich Bescheid geben.
        return _redirect_to_helper_detail_with_link(
            request, db, helper,
            flash="info: Email ist bereits verifiziert — keine Mail verschickt.",
        )

    # Frischen Token erzeugen und speichern (alter wird damit ungültig)
    helper.email_verification_token = generate_token()
    db.commit()

    base = str(request.base_url).rstrip("/")
    verify_url = f"{base}/verify/{helper.email_verification_token}"

    # Cc auf die Festival-Adresse, falls SMTP-Absender konfiguriert ist.
    cc_address = settings.SMTP_FROM_ADDRESS or None

    flash: str
    if settings.smtp_enabled:
        try:
            from ..email_sender import send_verification_email
            send_verification_email(helper, verify_url, cc=cc_address)
            cc_note = f" (Kopie an {cc_address})" if cc_address else ""
            flash = f"success: Verifikations-Mail an {helper.email} verschickt{cc_note}."
        except Exception as exc:  # noqa: BLE001
            print(f"[admin resend_verify] SMTP-Fehler: {exc}")
            flash = (f"error: SMTP-Fehler beim Versand: {exc}. "
                     f"Du kannst den Link manuell weitergeben: {verify_url}")
    else:
        # Ohne SMTP geben wir den Link direkt zurück, dann kann Admin ihn
        # über einen anderen Kanal verschicken.
        print(f"[admin resend_verify] Verifikations-Link für {helper.email}: {verify_url}")
        flash = (f"info: SMTP nicht konfiguriert – Mail wurde NICHT verschickt. "
                 f"Link manuell weitergeben: {verify_url}")

    return _redirect_to_helper_detail_with_link(request, db, helper, flash=flash)


def _redirect_to_helper_detail_with_link(request, db, helper, reset_url: str | None = None,
                                         flash: str | None = None):
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()
    all_roles = db.query(models.Role).join(models.Area).order_by(models.Area.sort_order, models.Role.sort_order).all()
    trust_ids = {t.role_id for t in helper.role_trusts}
    pref_areas = {p.area_id for p in helper.preferences}
    grouped_roles: dict[str, list[models.Role]] = {}
    for r in all_roles:
        grouped_roles.setdefault(r.area.name, []).append(r)
    assignments = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.helper_id == helper.id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
            joinedload(models.ShiftAssignment.role),
        )
        .all()
    )
    return templates.TemplateResponse(
        "admin/helper_detail.html",
        _ctx(
            request, helper=helper, days=days, areas=areas,
            all_roles=all_roles, grouped_roles=grouped_roles,
            trust_ids=trust_ids, pref_areas=pref_areas,
            assignments=assignments, reset_url=reset_url,
            admin_flash=flash,
        ),
    )


# ---------------------------------------------------------------------------
# Schichten / Dienstplan
# ---------------------------------------------------------------------------
@router.get("/shifts", response_class=HTMLResponse)
def shifts_list(
    request: Request,
    db: Session = Depends(get_db),
    area_id: str | None = Query(None),
    day_id: str | None = Query(None),
):
    if (r := require_admin_redirect(request)):
        return r

    area_id_int = _parse_int_or_none(area_id)
    day_id_int = _parse_int_or_none(day_id)

    query = db.query(models.Shift).options(
        joinedload(models.Shift.area),
        joinedload(models.Shift.day),
        joinedload(models.Shift.assignments).joinedload(models.ShiftAssignment.helper),
        joinedload(models.Shift.assignments).joinedload(models.ShiftAssignment.role),
    )
    if area_id_int:
        query = query.filter(models.Shift.area_id == area_id_int)
    if day_id_int:
        query = query.filter(models.Shift.day_id == day_id_int)
    shifts = query.all()

    days = db.query(models.FestivalDay).order_by(
        models.FestivalDay.sort_order, models.FestivalDay.date
    ).all()
    areas = db.query(models.Area).order_by(
        models.Area.sort_order, models.Area.name
    ).all()

    # Sortierschlüssel-Maps für Tag- und Bereichs-Reihenfolge
    day_order = {d.id: i for i, d in enumerate(days)}
    day_label_by_id = {d.id: d.label for d in days}
    area_order = {a.id: i for i, a in enumerate(areas)}

    # Struktur aufbauen: pro Bereich eine geordnete Liste von Tagen,
    # pro Tag die Schichten nach Startzeit. Alles fertig sortiert, damit
    # das Template nur noch rendern muss.
    #   grouped_areas = [
    #     {"area": <Area>, "n_shifts": int, "n_open": int,
    #      "days": [ {"day_label": str, "shifts": [<Shift>...]}, ... ] },
    #     ... ]
    tmp: dict[int, dict[int, list]] = {}
    for s in shifts:
        tmp.setdefault(s.area_id, {}).setdefault(s.day_id, []).append(s)

    grouped_areas = []
    area_by_id = {a.id: a for a in areas}
    for area_id_key in sorted(tmp.keys(), key=lambda aid: area_order.get(aid, 999)):
        days_map = tmp[area_id_key]
        day_blocks = []
        n_shifts = 0
        n_open = 0
        for day_id_key in sorted(days_map.keys(), key=lambda did: day_order.get(did, 999)):
            shift_list = sorted(days_map[day_id_key], key=lambda s: s.start_time)
            for s in shift_list:
                n_shifts += 1
                if len(s.assignments) < s.capacity:
                    n_open += 1
            day_blocks.append({
                "day_label": day_label_by_id.get(day_id_key, "?"),
                "shifts": shift_list,
            })
        grouped_areas.append({
            "area": area_by_id.get(area_id_key),
            "n_shifts": n_shifts,
            "n_open": n_open,
            "days": day_blocks,
        })

    return templates.TemplateResponse(
        "admin/shifts.html",
        _ctx(request, grouped_areas=grouped_areas, days=days, areas=areas,
             area_id=area_id_int, day_id=day_id_int),
    )


@router.post("/shifts/new")
async def shift_new(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    form = await request.form()
    from datetime import time as dtime
    shift = models.Shift(
        area_id=int(form.get("area_id")),
        day_id=int(form.get("day_id")),
        label=(form.get("label") or "").strip() or None,
        start_time=dtime.fromisoformat(form.get("start_time")),
        end_time=dtime.fromisoformat(form.get("end_time")),
        capacity=int(form.get("capacity") or 1),
    )
    db.add(shift)
    db.commit()
    return RedirectResponse(f"/admin/shifts/{shift.id}", status_code=303)


@router.get("/shifts/{shift_id}", response_class=HTMLResponse)
def shift_detail(shift_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    shift = db.get(models.Shift, shift_id)
    if not shift:
        return HTMLResponse("Nicht gefunden", status_code=404)

    # Kandidat:innen: Helfer:innen, die an diesem Tag verfügbar sind UND diesen Bereich als Wunsch haben
    candidates = (
        db.query(models.Helper)
        .join(models.Availability, models.Availability.helper_id == models.Helper.id)
        .join(models.HelperAreaPreference, models.HelperAreaPreference.helper_id == models.Helper.id)
        .filter(models.Availability.day_id == shift.day_id)
        .filter(models.HelperAreaPreference.area_id == shift.area_id)
        .options(
            joinedload(models.Helper.preferences),
            joinedload(models.Helper.role_trusts),
        )
        .distinct()
        .all()
    )
    # Nach Rang sortieren (niedriger = besser), dann Name
    def prefs_rank_for_area(h: models.Helper, area_id: int) -> int:
        for p in h.preferences:
            if p.area_id == area_id:
                return p.rank
        return 99
    candidates.sort(key=lambda h: (prefs_rank_for_area(h, shift.area_id), h.first_name))

    assigned_ids = {a.helper_id for a in shift.assignments}
    roles = db.query(models.Role).filter(models.Role.area_id == shift.area_id).order_by(models.Role.sort_order).all()

    # Pre-compute pro Kandidat:in: welche Rollen sind zugetraut, und daraus
    # der Anzeige-String. Spart den `trust_names.append(...)`-Hack im Template.
    role_info_by_helper: dict[int, dict] = {}
    role_name_by_id = {r.id: r.name for r in roles}
    for c in candidates:
        trusted_role_ids = [t.role_id for t in c.role_trusts if t.role_id in role_name_by_id]
        role_info_by_helper[c.id] = {
            "trusted_role_ids": set(trusted_role_ids),
            "trust_names": [role_name_by_id[rid] for rid in trusted_role_ids],
        }

    return templates.TemplateResponse(
        "admin/shift_detail.html",
        _ctx(
            request,
            shift=shift,
            candidates=candidates,
            assigned_ids=assigned_ids,
            roles=roles,
            role_info_by_helper=role_info_by_helper,
            prefs_rank_for_area=prefs_rank_for_area,
        ),
    )


@router.post("/shifts/{shift_id}/delete")
def shift_delete(shift_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    shift = db.get(models.Shift, shift_id)
    if shift:
        db.delete(shift)
        db.commit()
    return RedirectResponse("/admin/shifts", status_code=303)


@router.post("/shifts/{shift_id}/assign")
async def shift_assign(shift_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    form = await request.form()
    helper_id = int(form.get("helper_id"))
    role_id_str = form.get("role_id")
    role_id = int(role_id_str) if role_id_str else None

    existing = db.query(models.ShiftAssignment).filter_by(shift_id=shift_id, helper_id=helper_id).one_or_none()
    if existing:
        existing.role_id = role_id
    else:
        db.add(models.ShiftAssignment(shift_id=shift_id, helper_id=helper_id, role_id=role_id))
    db.commit()
    return RedirectResponse(f"/admin/shifts/{shift_id}", status_code=303)


@router.post("/shifts/{shift_id}/unassign/{helper_id}")
def shift_unassign(shift_id: int, helper_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    db.query(models.ShiftAssignment).filter_by(shift_id=shift_id, helper_id=helper_id).delete()
    db.commit()
    return RedirectResponse(f"/admin/shifts/{shift_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin: Schichten zwischen zwei Helfer:innen manuell tauschen
# (v.a. für Bar, die vom Self-Service-Board ausgeschlossen ist)
# ---------------------------------------------------------------------------
@router.get("/swap", response_class=HTMLResponse)
def admin_swap_page(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r

    # Alle Helfer:innen mit mindestens einer Zuweisung, plus deren Schichten.
    helpers = (
        db.query(models.Helper)
        .options(
            joinedload(models.Helper.shift_assignments)
            .joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.Helper.shift_assignments)
            .joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
        )
        .order_by(models.Helper.last_name, models.Helper.first_name)
        .all()
    )
    # Nur Helfer:innen mit Zuweisungen sind tauschbar.
    helpers_with_shifts = [h for h in helpers if h.shift_assignments]

    # Als JSON-freundliche Struktur fürs Template: pro Helfer die Assignments.
    def assignment_label(a):
        s = a.shift
        return f"{s.area.name} · {s.day.label} · {s.time_range}" + (f" · {s.label}" if s.label else "")

    helper_data = []
    for h in helpers_with_shifts:
        helper_data.append({
            "id": h.id,
            "name": f"{h.first_name} {h.last_name}",
            "assignments": [
                {"id": a.id, "label": assignment_label(a)}
                for a in sorted(h.shift_assignments, key=lambda a: (a.shift.day.sort_order, a.shift.start_time))
            ],
        })

    import json as _json
    flash = request.query_params.get("flash")
    flash_map = {
        "swapped": ("success", "Schichten erfolgreich getauscht."),
        "same_helper": ("error", "Bitte zwei verschiedene Helfer:innen wählen."),
        "missing": ("error", "Bitte für beide Seiten eine Schicht auswählen."),
        "not_found": ("error", "Eine der Zuweisungen wurde nicht gefunden."),
        "conflict": ("error", "Tausch nicht möglich: es entstünde ein zeitlicher Konflikt."),
    }
    flash_data = flash_map.get(flash)

    return templates.TemplateResponse(
        "admin/swap.html",
        _ctx(
            request,
            helper_data=helper_data,
            helper_data_json=_json.dumps(helper_data),
            flash=flash_data,
        ),
    )


@router.post("/swap")
async def admin_swap_do(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    form = await request.form()

    def _int(name):
        try:
            return int(form.get(name) or 0) or None
        except ValueError:
            return None

    assign_a_id = _int("assignment_a")
    assign_b_id = _int("assignment_b")

    if not assign_a_id or not assign_b_id:
        return RedirectResponse("/admin/swap?flash=missing", status_code=303)
    if assign_a_id == assign_b_id:
        return RedirectResponse("/admin/swap?flash=missing", status_code=303)

    a = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assign_a_id)
        .options(joinedload(models.ShiftAssignment.shift))
        .one_or_none()
    )
    b = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assign_b_id)
        .options(joinedload(models.ShiftAssignment.shift))
        .one_or_none()
    )
    if not a or not b:
        return RedirectResponse("/admin/swap?flash=not_found", status_code=303)
    if a.helper_id == b.helper_id:
        return RedirectResponse("/admin/swap?flash=same_helper", status_code=303)

    # Tausch: die beiden Helfer:innen wechseln ihre Schichten.
    helper_a_id = a.helper_id
    helper_b_id = b.helper_id

    # Konfliktprüfung: Bekommt A (der jetzt B's Schicht kriegt) einen Zeitkonflikt
    # mit einer seiner ANDEREN Schichten? Und umgekehrt.
    def _has_conflict(helper_id, new_shift, own_assignment_id):
        others = (
            db.query(models.ShiftAssignment)
            .filter(models.ShiftAssignment.helper_id == helper_id)
            .filter(models.ShiftAssignment.id != own_assignment_id)
            .options(joinedload(models.ShiftAssignment.shift))
            .all()
        )
        for o in others:
            if o.shift.day_id != new_shift.day_id:
                continue
            if (o.shift.start_time < new_shift.end_time
                    and new_shift.start_time < o.shift.end_time):
                return True
        return False

    # A bekommt b.shift, B bekommt a.shift
    if _has_conflict(helper_a_id, b.shift, a.id) or _has_conflict(helper_b_id, a.shift, b.id):
        return RedirectResponse("/admin/swap?flash=conflict", status_code=303)

    a.helper_id, b.helper_id = helper_b_id, helper_a_id

    # Verfügbarkeiten defensiv ergänzen
    for hid, shift in ((helper_b_id, a.shift), (helper_a_id, b.shift)):
        h = db.get(models.Helper, hid)
        avail = {av.day_id for av in h.availabilities}
        if shift.day_id not in avail:
            db.add(models.Availability(helper_id=hid, day_id=shift.day_id))

    db.commit()
    return RedirectResponse("/admin/swap?flash=swapped", status_code=303)


# ---------------------------------------------------------------------------
# Stammdaten (Tage, Bereiche, Rollen)
# ---------------------------------------------------------------------------
@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = (
        db.query(models.Area)
        .options(joinedload(models.Area.roles))
        .order_by(models.Area.sort_order, models.Area.name)
        .all()
    )
    return templates.TemplateResponse("admin/config.html", _ctx(request, days=days, areas=areas))


@router.post("/config/days/new")
async def config_day_new(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    from datetime import date as ddate
    form = await request.form()
    db.add(models.FestivalDay(
        date=ddate.fromisoformat(form.get("date")),
        label=(form.get("label") or "").strip() or form.get("date"),
        sort_order=int(form.get("sort_order") or 0),
    ))
    db.commit()
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/config/days/{day_id}/delete")
def config_day_delete(day_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    d = db.get(models.FestivalDay, day_id)
    if d:
        db.delete(d); db.commit()
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/config/areas/new")
async def config_area_new(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    form = await request.form()
    db.add(models.Area(
        name=(form.get("name") or "").strip(),
        description=(form.get("description") or "").strip() or None,
        sort_order=int(form.get("sort_order") or 0),
    ))
    db.commit()
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/config/areas/{area_id}/delete")
def config_area_delete(area_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    a = db.get(models.Area, area_id)
    if a:
        db.delete(a); db.commit()
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/config/areas/{area_id}/roles/new")
async def config_role_new(area_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    form = await request.form()
    db.add(models.Role(
        area_id=area_id,
        name=(form.get("name") or "").strip(),
        sort_order=int(form.get("sort_order") or 0),
    ))
    db.commit()
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/config/roles/{role_id}/delete")
def config_role_delete(role_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    r_obj = db.get(models.Role, role_id)
    if r_obj:
        db.delete(r_obj); db.commit()
    return RedirectResponse("/admin/config", status_code=303)


# ---------------------------------------------------------------------------
# Mail-Verteiler
# ---------------------------------------------------------------------------
@router.get("/mail", response_class=HTMLResponse)
def mail_page(
    request: Request,
    db: Session = Depends(get_db),
    day_id: str | None = Query(None),
    area_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
):
    if (r := require_admin_redirect(request)):
        return r

    day_id_int = _parse_int_or_none(day_id)
    area_id_int = _parse_int_or_none(area_id)

    query = db.query(models.Helper)
    if day_id_int:
        query = query.join(models.Availability).filter(models.Availability.day_id == day_id_int)
    if area_id_int:
        # Prio 5 ist der Default "egal" und bedeutet kein aktives
        # Interesse am Bereich. Beim Filtern zeigen wir nur Helfer:innen
        # mit Prio 1-4 für diesen Bereich.
        query = query.join(models.HelperAreaPreference).filter(
            models.HelperAreaPreference.area_id == area_id_int,
            models.HelperAreaPreference.rank < 5,
        )
    if status_filter:
        query = query.filter(models.Helper.status == status_filter)
    helpers = query.distinct().all()

    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()

    return templates.TemplateResponse(
        "admin/mail.html",
        _ctx(
            request,
            helpers=helpers,
            days=days,
            areas=areas,
            day_id=day_id_int,
            area_id=area_id_int,
            status_filter=status_filter,
        ),
    )


@router.post("/mail/send")
async def mail_send(request: Request, db: Session = Depends(get_db)):
    """Sendet Mail via SMTP. Wirft Fehler, wenn SMTP nicht konfiguriert ist."""
    if (r := require_admin_redirect(request)):
        return r
    from ..email_sender import send_personalized, render_template, MailError

    form = await request.form()
    subject = (form.get("subject") or "").strip()
    body_template = (form.get("body") or "").strip()
    recipient_ids = [int(x) for x in form.getlist("recipient_ids")]
    test_only = form.get("test_only") == "on"

    helpers = db.query(models.Helper).filter(models.Helper.id.in_(recipient_ids)).all()
    if test_only:
        # Nur an die Admin-Absenderadresse selbst
        helpers_for_send = [(settings.SMTP_FROM_ADDRESS, "Test", render_template(body_template, {
            "Vorname": "Test", "Nachname": "Empfänger", "FestivalName": settings.FESTIVAL_NAME,
        }))]
    else:
        helpers_for_send = [
            (h.email, h.first_name, render_template(body_template, {
                "Vorname": h.first_name,
                "Nachname": h.last_name,
                "FestivalName": settings.FESTIVAL_NAME,
            }))
            for h in helpers
        ]

    try:
        count, skipped = send_personalized(helpers_for_send, subject=subject)
        if skipped:
            skipped_lines = "\n".join(f"  • {addr}: {reason}" for addr, reason in skipped)
            message = (f"{count} Mail(s) versendet, {len(skipped)} übersprungen "
                       f"(fehlerhafte Adresse oder SMTP-Ablehnung):\n{skipped_lines}\n\n"
                       f"Tipp: Adresse(n) im Admin korrigieren und Mail erneut senden — "
                       f"oder die Person per Hand anschreiben.")
            success = "partial"
        else:
            message = f"{count} Mail(s) erfolgreich versendet."
            success = True
    except MailError as exc:
        message = str(exc)
        success = False

    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()
    return templates.TemplateResponse(
        "admin/mail.html",
        _ctx(
            request,
            helpers=helpers,
            days=days,
            areas=areas,
            day_id=None,
            area_id=None,
            status_filter=None,
            flash_message=message,
            flash_success=success,
        ),
    )


# ---------------------------------------------------------------------------
# CSV Import / Export
# ---------------------------------------------------------------------------
@router.get("/export/helpers.csv")
def export_helpers(request: Request, db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    helpers = (
        db.query(models.Helper)
        .options(
            joinedload(models.Helper.availabilities).joinedload(models.Availability.day),
            joinedload(models.Helper.preferences).joinedload(models.HelperAreaPreference.area),
        )
        .order_by(models.Helper.last_name)
        .all()
    )
    csv_text = helpers_to_csv(helpers)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="helpers.csv"'},
    )


@router.get("/export/emails.csv")
def export_emails(
    request: Request,
    db: Session = Depends(get_db),
    day_id: str | None = Query(None),
    area_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
):
    if (r := require_admin_redirect(request)):
        return r
    day_id_int = _parse_int_or_none(day_id)
    area_id_int = _parse_int_or_none(area_id)
    query = db.query(models.Helper)
    if day_id_int:
        query = query.join(models.Availability).filter(models.Availability.day_id == day_id_int)
    if area_id_int:
        # Prio 5 ist der Default "egal" und bedeutet kein aktives
        # Interesse am Bereich. Beim Filtern zeigen wir nur Helfer:innen
        # mit Prio 1-4 für diesen Bereich.
        query = query.join(models.HelperAreaPreference).filter(
            models.HelperAreaPreference.area_id == area_id_int,
            models.HelperAreaPreference.rank < 5,
        )
    if status_filter:
        query = query.filter(models.Helper.status == status_filter)
    helpers = query.distinct().all()
    return Response(
        content=emails_to_csv(helpers),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="emails.csv"'},
    )


@router.post("/import/helpers")
async def import_helpers(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if (r := require_admin_redirect(request)):
        return r
    content = (await file.read()).decode("utf-8", errors="replace")
    result = import_helpers_from_csv(db, content)
    msg = f"Import abgeschlossen: {result['created']} neu, {result['updated']} aktualisiert."
    if result["errors"]:
        msg += " Fehler: " + "; ".join(result["errors"][:5])
    return RedirectResponse(f"/admin/helpers?imported={msg}", status_code=303)
