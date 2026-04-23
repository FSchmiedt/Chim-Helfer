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
        query = query.join(models.HelperAreaPreference).filter(models.HelperAreaPreference.area_id == area_id_int)
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


def _redirect_to_helper_detail_with_link(request, db, helper, reset_url: str):
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

    # Gruppieren nach Bereich -> Tag. Wir konvertieren am Ende in reguläre
    # Dicts, damit das Template nicht mit defaultdict-Magie rumhantieren muss
    # (spart den fragilen `{% if x.update(...) %}`-Hack).
    grouped_dd: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for s in shifts:
        grouped_dd[s.area.name][s.day.label].append(s)
    for area_name in grouped_dd:
        for day_label in grouped_dd[area_name]:
            grouped_dd[area_name][day_label].sort(key=lambda s: s.start_time)
    grouped = {area: dict(days_map) for area, days_map in grouped_dd.items()}

    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    areas = db.query(models.Area).order_by(models.Area.sort_order, models.Area.name).all()

    return templates.TemplateResponse(
        "admin/shifts.html",
        _ctx(request, grouped=grouped, days=days, areas=areas, area_id=area_id_int, day_id=day_id_int),
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
        query = query.join(models.HelperAreaPreference).filter(models.HelperAreaPreference.area_id == area_id_int)
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
        count = send_personalized(helpers_for_send, subject=subject)
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
        query = query.join(models.HelperAreaPreference).filter(models.HelperAreaPreference.area_id == area_id_int)
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
