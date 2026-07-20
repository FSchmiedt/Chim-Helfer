"""Helfer:innen-Bereich: Dashboard, Schichten, Tausch-Board, Tausch-Anfragen.

Alle Routen hier setzen eine Helfer-Session voraus. Die Routen sind absichtlich
klein gehalten — die meiste Komplexität liegt in den Validierungs-Helpern unten
(Eigentümerschaft prüfen, Konflikte erkennen).
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..auth import HELPER_COOKIE_NAME, get_current_helper, require_helper_redirect
from ..config import settings
from ..database import get_db
from ..shift_log import log_shift_change, log_transfer


# Lokal definiert, damit nicht zirkulär auf public.py zugegriffen werden muss.
PASSWORD_MIN_LEN = 8

router = APIRouter(tags=["me"])
templates = Jinja2Templates(directory="app/templates")


def _ctx(request: Request, helper: models.Helper, **extra) -> dict:
    ctx = {
        "request": request,
        "festival_name": settings.FESTIVAL_NAME,
        "helper": helper,
        "current_helper": helper,  # für base.html-Navigation
        "smtp_enabled": settings.smtp_enabled,
    }
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/me", response_class=HTMLResponse)
def me_dashboard(request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    # Eigene Zuweisungen inkl. ob gerade ein offenes Angebot dran hängt
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
    # Sortieren nach Tag-sort_order dann Startzeit
    assignments.sort(key=lambda a: (a.shift.day.sort_order, a.shift.day.date, a.shift.start_time))

    # Zu jeder Assignment das aktive Swap-Angebot (falls offen) holen
    assignment_ids = [a.id for a in assignments]
    my_offers_by_assignment: dict[int, models.ShiftSwapOffer] = {}
    if assignment_ids:
        for off in (
            db.query(models.ShiftSwapOffer)
            .filter(models.ShiftSwapOffer.assignment_id.in_(assignment_ids))
            .filter(models.ShiftSwapOffer.status == "open")
            .all()
        ):
            my_offers_by_assignment[off.assignment_id] = off

    # Zu jeder Assignment ggf. eine offene direkte Anfrage
    my_open_requests_by_assignment: dict[int, list[models.ShiftSwapRequest]] = {}
    if assignment_ids:
        for req in (
            db.query(models.ShiftSwapRequest)
            .filter(models.ShiftSwapRequest.from_assignment_id.in_(assignment_ids))
            .filter(models.ShiftSwapRequest.status == "pending")
            .options(joinedload(models.ShiftSwapRequest.to_helper))
            .all()
        ):
            my_open_requests_by_assignment.setdefault(req.from_assignment_id, []).append(req)

    # Eingehende offene Anfragen
    incoming = (
        db.query(models.ShiftSwapRequest)
        .filter(models.ShiftSwapRequest.to_helper_id == helper.id)
        .filter(models.ShiftSwapRequest.status == "pending")
        .options(
            joinedload(models.ShiftSwapRequest.from_helper),
            joinedload(models.ShiftSwapRequest.from_assignment).joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftSwapRequest.from_assignment).joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
            joinedload(models.ShiftSwapRequest.from_assignment).joinedload(models.ShiftAssignment.role),
        )
        .order_by(models.ShiftSwapRequest.created_at.desc())
        .all()
    )

    # Anzahl offener Board-Angebote (für Teaser-Link)
    open_board_count = (
        db.query(models.ShiftSwapOffer)
        .filter(models.ShiftSwapOffer.status == "open")
        .filter(models.ShiftSwapOffer.offered_by_helper_id != helper.id)
        .count()
    )

    # Besuch der eigenen Übersicht protokollieren (Ersatz für Lesebestätigung).
    # Bewusst ohne eigenes try/except: schlägt das commit fehl, soll der normale
    # Fehlerweg greifen statt still zu schlucken.
    helper.last_me_at = datetime.utcnow()
    helper.me_view_count = (helper.me_view_count or 0) + 1
    db.commit()

    # Tage für Availability-Anzeige
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    avail_day_ids = {a.day_id for a in helper.availabilities}

    # Self-Signup-Link anzeigen, wenn Schichtplan freigegeben ist (zeitgesteuert
    # oder manuell) ODER die Person ein Test-Nutzer ist.
    shift_signup_open = (
        settings.shift_signup_effective_open
        or helper.email.lower() in settings.shift_signup_preview_emails
    )

    return templates.TemplateResponse(
        "helper_dashboard.html",
        _ctx(
            request, helper,
            assignments=assignments,
            my_offers_by_assignment=my_offers_by_assignment,
            my_open_requests_by_assignment=my_open_requests_by_assignment,
            incoming_requests=incoming,
            open_board_count=open_board_count,
            days=days,
            avail_day_ids=avail_day_ids,
            shift_signup_open=shift_signup_open,
            swap_excluded_areas=settings.swap_excluded_areas,
        ),
    )


# ---------------------------------------------------------------------------
# Passwort ändern
# ---------------------------------------------------------------------------
@router.get("/me/password", response_class=HTMLResponse)
def me_password_form(request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir
    return templates.TemplateResponse(
        "helper_password.html",
        _ctx(request, helper, error=None, success=False, password_min_len=PASSWORD_MIN_LEN),
    )


@router.post("/me/password", response_class=HTMLResponse)
async def me_password_submit(request: Request, db: Session = Depends(get_db)):
    from ..passwords import hash_password, verify_password

    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    form = await request.form()
    current = form.get("current_password") or ""
    new = form.get("new_password") or ""
    new_confirm = form.get("new_password_confirm") or ""

    error: Optional[str] = None
    if not verify_password(current, helper.password_hash):
        error = "Aktuelles Passwort ist falsch."
    elif len(new) < PASSWORD_MIN_LEN:
        error = f"Neues Passwort muss mindestens {PASSWORD_MIN_LEN} Zeichen lang sein."
    elif new != new_confirm:
        error = "Neue Passwörter stimmen nicht überein."

    if error:
        return templates.TemplateResponse(
            "helper_password.html",
            _ctx(request, helper, error=error, success=False, password_min_len=PASSWORD_MIN_LEN),
            status_code=400,
        )

    helper.password_hash = hash_password(new)
    db.commit()
    return templates.TemplateResponse(
        "helper_password.html",
        _ctx(request, helper, error=None, success=True, password_min_len=PASSWORD_MIN_LEN),
    )


# ---------------------------------------------------------------------------
# „Nur eine Schicht"-Präferenz
# ---------------------------------------------------------------------------
@router.post("/me/shift-preference")
async def me_shift_preference(request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir
    form = await request.form()
    helper.wants_only_one_shift = form.get("wants_only_one_shift") == "on"
    db.commit()
    return RedirectResponse("/me?shift_pref_saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Selbst-Eintragen in offene Schichten
# ---------------------------------------------------------------------------
@router.get("/schichten", response_class=HTMLResponse)
def shifts_signup_list(request: Request, db: Session = Depends(get_db)):
    from ..auth import is_admin
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    # Wer darf die Schichten sehen/buchen?
    #  - Alle, wenn der Self-Signup (zeitgesteuert oder manuell) offen ist
    #  - Admin immer (für Vorschau)
    #  - Preview-Email-Adressen (Test-Nutzer) immer
    is_admin_viewer = is_admin(request)
    is_preview_user = helper.email.lower() in settings.shift_signup_preview_emails
    signup_open = settings.shift_signup_effective_open
    may_view = signup_open or is_admin_viewer or is_preview_user
    if not may_view:
        return templates.TemplateResponse(
            "helper_shifts_locked.html",
            _ctx(request, helper, opens_at=settings._parse_signup_open_at()),
        )

    # Welche Bereiche darf der Helfer:in sehen? Nur Wunschbereiche.
    pref_area_ids = {p.area_id for p in helper.preferences}
    # Rang pro Bereich für die Sortierung (1 = wichtigste Wahl, kommt zuerst)
    rank_by_area = {p.area_id: p.rank for p in helper.preferences}

    if not pref_area_ids:
        shifts = []
    else:
        shifts = (
            db.query(models.Shift)
            .filter(models.Shift.area_id.in_(pref_area_ids))
            .options(
                joinedload(models.Shift.area),
                joinedload(models.Shift.day),
                joinedload(models.Shift.assignments),
            )
            .all()
        )

    # Eigene Zuweisungen für Konfliktprüfung
    my_assignments = list(helper.shift_assignments)
    avail_day_ids = {a.day_id for a in helper.availabilities}

    # Schichten anreichern: ist sie noch frei, kann ich sie nehmen?
    enriched = []
    for s in shifts:
        n_assigned = len(s.assignments)
        is_free = n_assigned < s.capacity
        already_mine = any(a.shift_id == s.id for a in my_assignments)
        # Volle Schichten gar nicht erst anzeigen – außer der Helfer:in ist
        # selbst dabei, dann bleibt die Schicht mit „du bist dabei" sichtbar.
        if not is_free and not already_mine:
            continue
        can_take, reason = _can_helper_take_shift_for_signup(
            helper, s, avail_day_ids, my_assignments,
        )
        enriched.append({
            "shift": s,
            "n_assigned": n_assigned,
            "is_free": is_free,
            "already_mine": already_mine,
            "can_take": can_take and is_free and not already_mine,
            "reason": reason if not (can_take and is_free) else "",
        })

    # Gruppieren nach Tag, innerhalb des Tages nach Wunsch-Rang (1 zuerst)
    days = db.query(models.FestivalDay).order_by(
        models.FestivalDay.sort_order, models.FestivalDay.date
    ).all()
    # by_day_area: dict[day_id] -> list[(area, items)] in Rang-Reihenfolge
    grouped_by_day: dict[int, dict[int, list]] = {}
    for item in enriched:
        s = item["shift"]
        grouped_by_day.setdefault(s.day_id, {}).setdefault(s.area_id, []).append(item)

    # Pro Tag die Bereiche nach Wunsch-Rang sortieren (1 zuerst, dann 2, ...).
    # Innerhalb eines Bereichs sortieren wir die Schichten im Template
    # nach Startzeit.
    by_day_ordered_areas: dict[int, list[tuple]] = {}
    for day_id, areas_dict in grouped_by_day.items():
        ordered = sorted(
            areas_dict.items(),
            key=lambda kv: (rank_by_area.get(kv[0], 99), kv[1][0]["shift"].area.name),
        )
        # ordered ist Liste von (area_id, items). Wir ergänzen den Rang fürs Template.
        by_day_ordered_areas[day_id] = [
            {"area_id": aid, "area_name": its[0]["shift"].area.name,
             "rank": rank_by_area.get(aid), "shift_items": its}
            for aid, its in ordered
        ]

    # Hat Helfer:in „nur eine Schicht" gewählt UND schon eine?
    has_max = helper.wants_only_one_shift and len(my_assignments) >= 1

    # Flash aus Query-Params
    flash_kind = request.query_params.get("flash")
    flash_messages = {
        "taken": ("success", "Schicht eingetragen. Sie steht in deinem Bereich."),
        "race": ("warning", "Diese Schicht wurde gerade von jemand anderem genommen — schau dir die anderen freien Schichten an."),
        "conflict": ("warning", "Du hast zur gleichen Zeit schon eine Schicht."),
        "already": ("info", "Du bist dieser Schicht schon zugewiesen."),
        "not_wanted_area": ("warning", "Diese Schicht ist nicht in deinen Wunschbereichen."),
        "max_reached": ("info", "Du hast 'nur eine Schicht' gewählt und hast bereits eine. Häkchen unten entfernen, falls du mehr machen willst."),
        "locked": ("warning", "Der Schichtplan ist noch nicht freigegeben."),
    }
    flash = flash_messages.get(flash_kind)

    # Vorschau-Modus: Admin ODER Test-Nutzer sieht die Seite, obwohl der
    # Signup öffentlich noch nicht offen ist.
    is_preview_mode = (not signup_open) and (is_admin_viewer or is_preview_user)
    # Test-Nutzer dürfen im Vorschau-Modus buchen (zum Testen), reine
    # Admin-Betrachtung nicht (Admin ist nicht als Helfer:in gemeint).
    preview_can_book = is_preview_user

    return templates.TemplateResponse(
        "helper_shifts_signup.html",
        _ctx(
            request, helper,
            days=days,
            by_day_ordered_areas=by_day_ordered_areas,
            has_max=has_max,
            flash=flash,
            pref_area_ids=pref_area_ids,
            is_admin_preview=is_preview_mode,
            preview_can_book=preview_can_book,
        ),
    )


@router.post("/schichten/{shift_id}/buchen")
def shift_signup_book(shift_id: int, request: Request, db: Session = Depends(get_db)):
    """Helfer:in trägt sich selbst in eine offene Schicht ein.

    Race-Condition-Schutz:
    1. UniqueConstraint(shift_id, helper_id) verhindert Doppelbelegung der Person.
    2. Wir prüfen Kapazität innerhalb der Transaktion und commit'en sofort.
       Bei gleichzeitigen Anfragen kann es passieren, dass zwei Personen
       die Kapazität gerade noch frei sehen und beide commit'en. Wir
       erkennen das, indem wir NACH dem Insert nochmal zählen und rollbacken,
       falls die Kapazität jetzt überschritten ist.
    """
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    # Sperre: Self-Signup nur, wenn offen (zeitgesteuert/manuell) ODER die
    # Person ist Test-Nutzer (Preview) bzw. Admin. Test-Nutzer dürfen bewusst
    # buchen, damit der echte Flow testbar ist — Buchungen kann der Admin
    # danach wieder entfernen.
    from ..auth import is_admin
    may_book = (
        settings.shift_signup_effective_open
        or is_admin(request)
        or helper.email.lower() in settings.shift_signup_preview_emails
    )
    if not may_book:
        return RedirectResponse("/schichten?flash=locked", status_code=303)

    shift = (
        db.query(models.Shift)
        .filter(models.Shift.id == shift_id)
        .options(joinedload(models.Shift.area), joinedload(models.Shift.day))
        .one_or_none()
    )
    if not shift:
        return RedirectResponse("/schichten?flash=race", status_code=303)

    # 1. Nur Wunschbereiche
    pref_area_ids = {p.area_id for p in helper.preferences}
    if shift.area_id not in pref_area_ids:
        return RedirectResponse("/schichten?flash=not_wanted_area", status_code=303)

    # 2. „Nur eine Schicht"-Limit beachten
    my_assignments = list(helper.shift_assignments)
    if helper.wants_only_one_shift and len(my_assignments) >= 1:
        return RedirectResponse("/schichten?flash=max_reached", status_code=303)

    # 3. Schon eingetragen?
    if any(a.shift_id == shift_id for a in my_assignments):
        return RedirectResponse("/schichten?flash=already", status_code=303)

    # 4. Zeitkonflikt mit eigener anderer Schicht?
    avail_day_ids = {a.day_id for a in helper.availabilities}
    can, reason = _can_helper_take_shift_for_signup(
        helper, shift, avail_day_ids, my_assignments,
    )
    if not can:
        return RedirectResponse(f"/schichten?flash={reason}", status_code=303)

    # 5. Atomischer Buchungsversuch mit Race-Schutz
    #    Wir nutzen einen einfachen "INSERT + check"-Ansatz, der mit SQLite
    #    UND Postgres funktioniert ohne explizite Locks. Bei Postgres reicht
    #    das wegen Read-Committed-Isolation: zwei parallele Inserts sehen
    #    beide n_before, einer schreibt zuerst, danach zählt der zweite
    #    eine zu viel und macht rollback.
    try:
        n_before = db.query(models.ShiftAssignment).filter(
            models.ShiftAssignment.shift_id == shift_id
        ).count()
        if n_before >= shift.capacity:
            return RedirectResponse("/schichten?flash=race", status_code=303)

        db.add(models.ShiftAssignment(
            shift_id=shift_id, helper_id=helper.id, role_id=None,
        ))
        db.flush()

        n_after = db.query(models.ShiftAssignment).filter(
            models.ShiftAssignment.shift_id == shift_id
        ).count()
        if n_after > shift.capacity:
            # Wettrennen verloren — Insert zurückrollen
            db.rollback()
            return RedirectResponse("/schichten?flash=race", status_code=303)

        # Tag stillschweigend zur Verfügbarkeit hinzufügen, falls fehlend
        if shift.day_id not in avail_day_ids:
            db.add(models.Availability(helper_id=helper.id, day_id=shift.day_id))

        log_shift_change(
            db, helper_id=helper.id, shift=shift,
            action="assigned", source="self_signup",
        )

        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse("/schichten?flash=race", status_code=303)

    return RedirectResponse("/schichten?flash=taken", status_code=303)


def _can_helper_take_shift_for_signup(
    helper: models.Helper,
    shift: models.Shift,
    avail_day_ids: set[int],
    my_assignments: list[models.ShiftAssignment],
) -> tuple[bool, str]:
    """Wie `_can_helper_take_shift` für Tausch, aber gegen ein `Shift`-Objekt
    statt eine Assignment. Logik identisch: Doppelung + Zeitkonflikt."""
    for other in my_assignments:
        if other.shift_id == shift.id:
            return False, "already"
        if other.shift.day_id != shift.day_id:
            continue
        if _times_overlap(other.shift.start_time, other.shift.end_time,
                          shift.start_time, shift.end_time):
            return False, "conflict"
    return True, "ok"


# ---------------------------------------------------------------------------
# Selbst aus einer Schicht austragen (ohne Tausch)
# ---------------------------------------------------------------------------
@router.post("/me/assignments/{assignment_id}/withdraw")
def me_withdraw_assignment(assignment_id: int, request: Request, background_tasks: BackgroundTasks,
                            db: Session = Depends(get_db)):
    """Helfer:in trägt sich selbst aus einer Schicht aus – ohne Tausch.

    Funktioniert für alle Bereiche gleich, auch Bar: die Zuweisung wird
    gelöscht, die Schicht ist damit sofort wieder frei. Danach geht eine
    Info-Mail ans zuständige Org-Postfach raus (bar@ bei Bar, sonst helfen@),
    damit das Team nicht jede Austragung einzeln im Tool nachschauen muss.
    """
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    assignment = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assignment_id)
        .options(joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
                 joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day))
        .one_or_none()
    )
    if not assignment or assignment.helper_id != helper.id:
        return RedirectResponse("/me?error=not_your_assignment", status_code=303)

    # Offene Board-Angebote und ausgehende Tausch-Anfragen zu genau dieser
    # Schicht aufräumen, damit keine ins Leere zeigenden Einträge übrig bleiben.
    db.query(models.ShiftSwapOffer).filter(
        models.ShiftSwapOffer.assignment_id == assignment_id
    ).delete(synchronize_session=False)
    db.query(models.ShiftSwapRequest).filter(
        models.ShiftSwapRequest.from_assignment_id == assignment_id
    ).delete(synchronize_session=False)

    log_shift_change(
        db, helper_id=helper.id, shift=assignment.shift,
        action="unassigned", source="self_withdraw", role=assignment.role,
    )

    from ..email_sender import build_org_withdraw_notice, deliver, send_in_background
    org_msg = build_org_withdraw_notice(helper, assignment.shift)

    db.delete(assignment)
    db.commit()

    send_in_background(background_tasks, deliver, org_msg, label="withdraw_notice")
    return RedirectResponse("/me?withdrawn=1", status_code=303)


# ---------------------------------------------------------------------------
# Schicht aufs Board stellen / zurückziehen
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tausch-Matching-Logik
# ---------------------------------------------------------------------------
def _area_is_swap_excluded(area) -> bool:
    return area.name.strip().lower() in settings.swap_excluded_areas


def _offer_matching_assignments(offer, candidate_assignments):
    """Welche der `candidate_assignments` (von Person B) passen zu A's Angebot?

    Regeln:
      - want_type "day": B-Schicht muss am gewünschten Tag beginnen
      - want_type "shifts": B-Schicht muss eine der angehakten Wunschschichten sein
      - ausgeschlossene Bereiche (Bar) werden nie als Gegenschicht akzeptiert
      - die B-Schicht darf nicht dieselbe sein wie A's angebotene Schicht
    Gibt die Liste der passenden Assignments zurück.
    """
    offered_shift_id = offer.assignment.shift_id
    matches = []
    if offer.want_type == "day":
        for a in candidate_assignments:
            if a.shift_id == offered_shift_id:
                continue
            if _area_is_swap_excluded(a.shift.area):
                continue
            if a.shift.day_id == offer.wanted_day_id:
                matches.append(a)
    else:  # "shifts"
        wanted_ids = {w.shift_id for w in offer.wanted_shifts}
        for a in candidate_assignments:
            if a.shift_id == offered_shift_id:
                continue
            if _area_is_swap_excluded(a.shift.area):
                continue
            if a.shift_id in wanted_ids:
                matches.append(a)
    return matches


# ---------------------------------------------------------------------------
# Schicht aufs Board stellen: Formular + Anlegen
# ---------------------------------------------------------------------------
@router.get("/me/assignments/{assignment_id}/offer", response_class=HTMLResponse)
def me_offer_form(assignment_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    assignment = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assignment_id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
        )
        .one_or_none()
    )
    if not assignment or assignment.helper_id != helper.id:
        return RedirectResponse("/me?error=not_your_assignment", status_code=303)

    # Bar (o.a.) darf nicht aufs Board.
    if _area_is_swap_excluded(assignment.shift.area):
        return RedirectResponse("/me?error=area_excluded", status_code=303)

    # Bereits ein offenes Angebot?
    existing = (
        db.query(models.ShiftSwapOffer)
        .filter(models.ShiftSwapOffer.assignment_id == assignment_id)
        .filter(models.ShiftSwapOffer.status == "open")
        .one_or_none()
    )
    if existing:
        return RedirectResponse("/me", status_code=303)

    days = db.query(models.FestivalDay).order_by(
        models.FestivalDay.sort_order, models.FestivalDay.date
    ).all()

    # Für "genau diese Schichten": alle Schichten anbieten, gruppiert nach
    # Tag, ohne ausgeschlossene Bereiche und ohne A's eigene Schicht.
    excluded = settings.swap_excluded_areas
    all_shifts = (
        db.query(models.Shift)
        .options(joinedload(models.Shift.area), joinedload(models.Shift.day))
        .all()
    )
    selectable = [
        s for s in all_shifts
        if s.area.name.strip().lower() not in excluded
        and s.id != assignment.shift_id
    ]
    # Gruppieren nach Tag, dann Startzeit
    day_order = {d.id: i for i, d in enumerate(days)}
    shifts_by_day: dict[int, list] = {}
    for s in selectable:
        shifts_by_day.setdefault(s.day_id, []).append(s)
    for lst in shifts_by_day.values():
        lst.sort(key=lambda s: (s.area.sort_order, s.start_time))
    ordered_days = sorted(shifts_by_day.keys(), key=lambda did: day_order.get(did, 99))

    return templates.TemplateResponse(
        "helper_offer_form.html",
        _ctx(
            request, helper,
            assignment=assignment,
            days=days,
            shifts_by_day=shifts_by_day,
            ordered_day_ids=ordered_days,
        ),
    )


@router.post("/me/assignments/{assignment_id}/offer")
async def me_offer_shift(assignment_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    assignment = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assignment_id)
        .options(joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area))
        .one_or_none()
    )
    if not assignment or assignment.helper_id != helper.id:
        return RedirectResponse("/me?error=not_your_assignment", status_code=303)

    if _area_is_swap_excluded(assignment.shift.area):
        return RedirectResponse("/me?error=area_excluded", status_code=303)

    existing = (
        db.query(models.ShiftSwapOffer)
        .filter(models.ShiftSwapOffer.assignment_id == assignment_id)
        .filter(models.ShiftSwapOffer.status == "open")
        .one_or_none()
    )
    if existing:
        return RedirectResponse("/me", status_code=303)

    form = await request.form()
    message = (form.get("message") or "").strip() or None
    want_type = form.get("want_type") or "day"
    allow_giveaway = form.get("allow_giveaway") == "on"

    offer = models.ShiftSwapOffer(
        assignment_id=assignment_id,
        offered_by_helper_id=helper.id,
        message=message,
        status="open",
        want_type=want_type if want_type in ("day", "shifts") else "day",
        allow_giveaway=allow_giveaway,
    )

    if offer.want_type == "day":
        try:
            offer.wanted_day_id = int(form.get("wanted_day_id") or 0) or None
        except ValueError:
            offer.wanted_day_id = None
        # Validierung: Tag muss existieren
        if not offer.wanted_day_id:
            return RedirectResponse(
                f"/me/assignments/{assignment_id}/offer?error=no_day", status_code=303
            )
    else:  # "shifts"
        raw_ids = form.getlist("wanted_shift_ids")
        wanted_ids = []
        for x in raw_ids:
            try:
                wanted_ids.append(int(x))
            except ValueError:
                continue
        if not wanted_ids and not allow_giveaway:
            # Keine Wunschschicht + kein Giveaway = sinnloses Angebot
            return RedirectResponse(
                f"/me/assignments/{assignment_id}/offer?error=no_shifts", status_code=303
            )

    db.add(offer)
    db.flush()

    if offer.want_type == "shifts":
        # Wunschschichten anlegen (ausgeschlossene Bereiche rausfiltern)
        excluded = settings.swap_excluded_areas
        valid_shifts = (
            db.query(models.Shift)
            .filter(models.Shift.id.in_(wanted_ids))
            .options(joinedload(models.Shift.area))
            .all()
        )
        for s in valid_shifts:
            if s.area.name.strip().lower() in excluded:
                continue
            if s.id == assignment.shift_id:
                continue
            db.add(models.ShiftSwapOfferWantedShift(offer_id=offer.id, shift_id=s.id))

    db.commit()
    return RedirectResponse("/me", status_code=303)


@router.post("/me/offers/{offer_id}/cancel")
def me_cancel_offer(offer_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    offer = db.get(models.ShiftSwapOffer, offer_id)
    if not offer or offer.offered_by_helper_id != helper.id or offer.status != "open":
        return RedirectResponse("/me", status_code=303)

    offer.status = "cancelled"
    offer.resolved_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/me", status_code=303)


# ---------------------------------------------------------------------------
# Board anzeigen / Angebote übernehmen
# ---------------------------------------------------------------------------
@router.get("/board", response_class=HTMLResponse)
def board(request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    offers = (
        db.query(models.ShiftSwapOffer)
        .filter(models.ShiftSwapOffer.status == "open")
        .options(
            joinedload(models.ShiftSwapOffer.offered_by),
            joinedload(models.ShiftSwapOffer.assignment).joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftSwapOffer.assignment).joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
            joinedload(models.ShiftSwapOffer.assignment).joinedload(models.ShiftAssignment.role),
            joinedload(models.ShiftSwapOffer.wanted_day),
            joinedload(models.ShiftSwapOffer.wanted_shifts).joinedload(models.ShiftSwapOfferWantedShift.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftSwapOffer.wanted_shifts).joinedload(models.ShiftSwapOfferWantedShift.shift).joinedload(models.Shift.day),
        )
        .order_by(models.ShiftSwapOffer.created_at.desc())
        .all()
    )
    foreign_offers = [o for o in offers if o.offered_by_helper_id != helper.id]
    own_offers = [o for o in offers if o.offered_by_helper_id == helper.id]

    # Meine eigenen Schichten (als mögliche Gegenschichten), mit geladenen shifts
    my_assignments = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.helper_id == helper.id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
        )
        .all()
    )

    # Pro fremdem Angebot: welche meiner Schichten passen als Gegenschicht?
    # + kann ich A's Schicht überhaupt zeitlich nehmen (kein Konflikt)?
    offer_options: dict[int, dict] = {}
    avail_day_ids = {a.day_id for a in helper.availabilities}
    for off in foreign_offers:
        # Passt A's Schicht in meinen Plan? (Zeitkonflikt-Check gegen die
        # Schichten, die ich BEHALTE — vereinfachend prüfen wir gegen alle;
        # die Gegenschicht, die ich abgebe, wird beim Take ausgenommen.)
        matching = _offer_matching_assignments(off, my_assignments)
        offer_options[off.id] = {
            "matching": matching,          # meine abgebbaren Schichten
            "can_giveaway": off.allow_giveaway,
        }

    return templates.TemplateResponse(
        "helper_board.html",
        _ctx(
            request, helper,
            foreign_offers=foreign_offers,
            own_offers=own_offers,
            offer_options=offer_options,
        ),
    )


@router.post("/board/{offer_id}/take")
async def board_take(offer_id: int, request: Request, background_tasks: BackgroundTasks,
                     db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    offer = (
        db.query(models.ShiftSwapOffer)
        .filter(models.ShiftSwapOffer.id == offer_id)
        .options(
            joinedload(models.ShiftSwapOffer.assignment).joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftSwapOffer.wanted_shifts),
        )
        .one_or_none()
    )
    if not offer or offer.status != "open":
        return RedirectResponse("/board?error=not_available", status_code=303)
    if offer.offered_by_helper_id == helper.id:
        return RedirectResponse("/board?error=own_offer", status_code=303)

    form = await request.form()
    give_raw = (form.get("give_assignment_id") or "").strip()

    # Meine Schichten laden
    my_assignments = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.helper_id == helper.id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
        )
        .all()
    )
    matching = _offer_matching_assignments(offer, my_assignments)

    give_assignment = None
    if give_raw and give_raw != "giveaway":
        try:
            give_id = int(give_raw)
        except ValueError:
            return RedirectResponse("/board?error=bad_choice", status_code=303)
        # Muss eine meiner passenden Schichten sein
        give_assignment = next((a for a in matching if a.id == give_id), None)
        if not give_assignment:
            return RedirectResponse("/board?error=no_match", status_code=303)
    else:
        # Giveaway gewählt (oder nichts) — nur erlaubt, wenn A das zulässt
        if not offer.allow_giveaway:
            # 1:1-Pflicht: wenn keine passende Schicht existiert, Fehlermeldung
            if not matching:
                return RedirectResponse("/board?error=need_match", status_code=303)
            return RedirectResponse("/board?error=choose_shift", status_code=303)

    a_assignment = offer.assignment  # A's Schicht, geht an mich (B)
    avail_day_ids = {a.day_id for a in helper.availabilities}

    # Zeitkonflikt-Prüfung: A's Schicht darf sich nicht mit meinen Schichten
    # überschneiden – außer mit der, die ich gerade abgebe.
    keep = [a for a in my_assignments if not (give_assignment and a.id == give_assignment.id)]
    for other in keep:
        if other.shift.day_id != a_assignment.shift.day_id:
            continue
        if _times_overlap(other.shift.start_time, other.shift.end_time,
                          a_assignment.shift.start_time, a_assignment.shift.end_time):
            return RedirectResponse("/board?error=conflict", status_code=303)

    # --- Tausch durchführen ---
    old_a_helper_id = a_assignment.helper_id  # = A
    # A's Schicht kommt zu mir (B)
    a_assignment.helper_id = helper.id
    # meine Gegenschicht (falls vorhanden) geht an A
    if give_assignment:
        give_assignment.helper_id = old_a_helper_id
        offer.taken_with_assignment_id = give_assignment.id
        # A war evtl. nicht als an dem Tag verfügbar markiert
        gday = give_assignment.shift.day_id
        a_avail = {av.day_id for av in db.get(models.Helper, old_a_helper_id).availabilities}
        if gday not in a_avail:
            db.add(models.Availability(helper_id=old_a_helper_id, day_id=gday))

    # Protokoll: A's Schicht geht an mich, meine Gegenschicht (falls es eine
    # gibt) an A. Jeweils zwei Zeilen, damit beide Historien vollständig sind.
    log_transfer(
        db, shift=a_assignment.shift,
        from_helper_id=old_a_helper_id, to_helper_id=helper.id,
        source="swap_board", role=a_assignment.role,
    )
    if give_assignment:
        log_transfer(
            db, shift=give_assignment.shift,
            from_helper_id=helper.id, to_helper_id=old_a_helper_id,
            source="swap_board", role=give_assignment.role,
        )

    offer.status = "taken"
    offer.taken_by_helper_id = helper.id
    offer.resolved_at = datetime.utcnow()

    # Ich (B) bin am Tag von A's Schicht jetzt verfügbar
    if a_assignment.shift.day_id not in avail_day_ids:
        db.add(models.Availability(helper_id=helper.id, day_id=a_assignment.shift.day_id))

    # Andere offene Requests/Offers auf die getauschten Assignments abbrechen
    involved = [a_assignment.id] + ([give_assignment.id] if give_assignment else [])
    db.query(models.ShiftSwapRequest).filter(
        models.ShiftSwapRequest.from_assignment_id.in_(involved),
        models.ShiftSwapRequest.status == "pending",
    ).update({"status": "cancelled", "resolved_at": datetime.utcnow()}, synchronize_session=False)
    if give_assignment:
        # Falls B seine Gegenschicht selbst auch aufs Board gestellt hatte
        db.query(models.ShiftSwapOffer).filter(
            models.ShiftSwapOffer.assignment_id == give_assignment.id,
            models.ShiftSwapOffer.status == "open",
        ).update({"status": "cancelled", "resolved_at": datetime.utcnow()}, synchronize_session=False)

    db.commit()

    # Mail an A
    if settings.smtp_enabled:
        # Text hier bauen (Session offen, Lazy-Loads gehen), Versand im Hintergrund.
        from ..email_sender import build_swap_taken_message, deliver, send_in_background
        old_helper = db.get(models.Helper, old_a_helper_id)
        if old_helper:
            msg = build_swap_taken_message(old_helper, helper, a_assignment)
            send_in_background(background_tasks, deliver, msg, label="board_take")

    return RedirectResponse("/me?taken=1", status_code=303)


# ---------------------------------------------------------------------------
# Direkte Tausch-Anfrage
# ---------------------------------------------------------------------------
@router.get("/me/assignments/{assignment_id}/swap", response_class=HTMLResponse)
def me_swap_form(assignment_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    assignment = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assignment_id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
            joinedload(models.ShiftAssignment.role),
        )
        .one_or_none()
    )
    if not assignment or assignment.helper_id != helper.id:
        return RedirectResponse("/me", status_code=303)

    return templates.TemplateResponse(
        "helper_swap_new.html",
        _ctx(request, helper, assignment=assignment, error=None, prefill_email=""),
    )


@router.post("/me/assignments/{assignment_id}/swap", response_class=HTMLResponse)
async def me_swap_submit(assignment_id: int, request: Request, background_tasks: BackgroundTasks,
                         db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    assignment = (
        db.query(models.ShiftAssignment)
        .filter(models.ShiftAssignment.id == assignment_id)
        .options(
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.area),
            joinedload(models.ShiftAssignment.shift).joinedload(models.Shift.day),
            joinedload(models.ShiftAssignment.role),
        )
        .one_or_none()
    )
    if not assignment or assignment.helper_id != helper.id:
        return RedirectResponse("/me", status_code=303)

    form = await request.form()
    target_email = (form.get("target_email") or "").strip().lower()
    message = (form.get("message") or "").strip() or None

    def error_tpl(err: str, status_code: int = 400) -> HTMLResponse:
        return templates.TemplateResponse(
            "helper_swap_new.html",
            _ctx(request, helper, assignment=assignment, error=err, prefill_email=target_email),
            status_code=status_code,
        )

    if not target_email:
        return error_tpl("Bitte gib die Email-Adresse deines:r Freund:in an.")
    if target_email == helper.email:
        return error_tpl("Du kannst nicht an dich selbst schicken.")

    target = db.query(models.Helper).filter(models.Helper.email == target_email).one_or_none()
    if not target:
        return error_tpl(
            "Diese Person ist nicht als Helfer:in angemeldet. "
            "Bitte frag sie, sich erst anzumelden, und versuch's dann nochmal."
        )

    # Bereits eine offene Anfrage an dieselbe Person für dieselbe Zuweisung?
    existing = (
        db.query(models.ShiftSwapRequest)
        .filter(models.ShiftSwapRequest.from_assignment_id == assignment_id)
        .filter(models.ShiftSwapRequest.to_helper_id == target.id)
        .filter(models.ShiftSwapRequest.status == "pending")
        .one_or_none()
    )
    if existing:
        return RedirectResponse("/me?duplicate=1", status_code=303)

    req = models.ShiftSwapRequest(
        from_assignment_id=assignment_id,
        from_helper_id=helper.id,
        to_helper_id=target.id,
        message=message,
        status="pending",
    )
    db.add(req)
    db.commit()

    if settings.smtp_enabled:
        from ..email_sender import build_swap_request_message, deliver, send_in_background
        msg = build_swap_request_message(target, helper, assignment, message)
        send_in_background(background_tasks, deliver, msg, label="swap_request")

    return RedirectResponse("/me?swap_sent=1", status_code=303)


@router.post("/me/swap-requests/{request_id}/accept")
def me_swap_accept(request_id: int, request: Request, background_tasks: BackgroundTasks,
                   db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    req = (
        db.query(models.ShiftSwapRequest)
        .filter(models.ShiftSwapRequest.id == request_id)
        .options(
            joinedload(models.ShiftSwapRequest.from_assignment).joinedload(models.ShiftAssignment.shift),
            joinedload(models.ShiftSwapRequest.from_helper),
        )
        .one_or_none()
    )
    if not req or req.to_helper_id != helper.id or req.status != "pending":
        return RedirectResponse("/me?error=invalid_request", status_code=303)

    assignment = req.from_assignment
    # Prüfen, ob der Ursprung noch stimmt (die Schicht könnte bereits weg sein)
    if assignment.helper_id != req.from_helper_id:
        req.status = "cancelled"
        req.resolved_at = datetime.utcnow()
        db.commit()
        return RedirectResponse("/me?error=origin_changed", status_code=303)

    avail_day_ids = {a.day_id for a in helper.availabilities}
    can, reason = _can_helper_take_shift(
        helper, assignment, avail_day_ids, list(helper.shift_assignments),
    )
    if not can:
        return RedirectResponse(f"/me?error={reason}", status_code=303)

    # Transfer durchführen
    old_helper_id = assignment.helper_id
    assignment.helper_id = helper.id
    req.status = "accepted"
    req.resolved_at = datetime.utcnow()

    log_transfer(
        db, shift=assignment.shift,
        from_helper_id=old_helper_id, to_helper_id=helper.id,
        source="swap_request", role=assignment.role,
    )

    # Availability nachziehen
    day_id = assignment.shift.day_id
    if day_id not in avail_day_ids:
        db.add(models.Availability(helper_id=helper.id, day_id=day_id))

    # Alle anderen offenen Anfragen/Angebote zu dieser Zuweisung abbrechen
    db.query(models.ShiftSwapRequest).filter(
        models.ShiftSwapRequest.from_assignment_id == assignment.id,
        models.ShiftSwapRequest.id != req.id,
        models.ShiftSwapRequest.status == "pending",
    ).update({"status": "cancelled", "resolved_at": datetime.utcnow()})
    db.query(models.ShiftSwapOffer).filter(
        models.ShiftSwapOffer.assignment_id == assignment.id,
        models.ShiftSwapOffer.status == "open",
    ).update({"status": "cancelled", "resolved_at": datetime.utcnow()})

    db.commit()

    if settings.smtp_enabled:
        from ..email_sender import build_swap_accepted_message, deliver, send_in_background
        requester = db.get(models.Helper, old_helper_id)
        if requester:
            msg = build_swap_accepted_message(requester, helper, assignment)
            send_in_background(background_tasks, deliver, msg, label="swap_accept")

    return RedirectResponse("/me?swap_accepted=1", status_code=303)


@router.post("/me/swap-requests/{request_id}/decline")
def me_swap_decline(request_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    req = db.get(models.ShiftSwapRequest, request_id)
    if not req or req.to_helper_id != helper.id or req.status != "pending":
        return RedirectResponse("/me", status_code=303)

    req.status = "declined"
    req.resolved_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/me?declined=1", status_code=303)


@router.post("/me/swap-requests/{request_id}/cancel")
def me_swap_cancel(request_id: int, request: Request, db: Session = Depends(get_db)):
    """Der:die Anfragende zieht die eigene Anfrage zurück."""
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    req = db.get(models.ShiftSwapRequest, request_id)
    if not req or req.from_helper_id != helper.id or req.status != "pending":
        return RedirectResponse("/me", status_code=303)

    req.status = "cancelled"
    req.resolved_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/me?cancelled=1", status_code=303)


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------
def _can_helper_take_shift(
    helper: models.Helper,
    assignment: models.ShiftAssignment,
    avail_day_ids: set[int],
    my_other_assignments: list[models.ShiftAssignment],
) -> tuple[bool, str]:
    """Prüft, ob `helper` die Schicht zu `assignment` übernehmen kann.

    Regeln:
    - Nicht bereits selbst dieser Schicht zugewiesen.
    - Keine zeitliche Überschneidung mit einer bestehenden Zuweisung am
      gleichen Tag.

    Availability-Status ist bewusst KEIN harter Blocker: wer eine Schicht
    übernimmt, MACHT sich verfügbar. Wir haken das beim eigentlichen Transfer
    in der Availability-Tabelle nach.
    """
    shift = assignment.shift
    # Bereits auf derselben Schicht?
    for other in my_other_assignments:
        if other.shift_id == shift.id:
            return False, "already_assigned"

    # Zeitliche Überschneidung am selben Tag?
    for other in my_other_assignments:
        if other.shift.day_id != shift.day_id:
            continue
        if _times_overlap(other.shift.start_time, other.shift.end_time, shift.start_time, shift.end_time):
            return False, "time_conflict"

    return True, "ok"


def _times_overlap(a_start: dtime, a_end: dtime, b_start: dtime, b_end: dtime) -> bool:
    """Einfacher Überschneidungs-Check (ignoriert Schichten über Mitternacht).

    Für das Festival reicht das: alle Schichten starten und enden am gleichen
    Kalendertag (FestivalDay). Mitternachts-Überschneidungen lösen wir durch
    separate FestivalDay-Einträge ('Fr bis Sa 6 Uhr' = zweiter Tag).
    """
    return a_start < b_end and b_start < a_end
