"""Helfer:innen-Bereich: Dashboard, Schichten, Tausch-Board, Tausch-Anfragen.

Alle Routen hier setzen eine Helfer-Session voraus. Die Routen sind absichtlich
klein gehalten — die meiste Komplexität liegt in den Validierungs-Helpern unten
(Eigentümerschaft prüfen, Konflikte erkennen).
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..auth import HELPER_COOKIE_NAME, get_current_helper, require_helper_redirect
from ..config import settings
from ..database import get_db


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

    # Tage für Availability-Anzeige
    days = db.query(models.FestivalDay).order_by(models.FestivalDay.sort_order, models.FestivalDay.date).all()
    avail_day_ids = {a.day_id for a in helper.availabilities}

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
# Schicht aufs Board stellen / zurückziehen
# ---------------------------------------------------------------------------
@router.post("/me/assignments/{assignment_id}/offer")
async def me_offer_shift(assignment_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    assignment = db.get(models.ShiftAssignment, assignment_id)
    if not assignment or assignment.helper_id != helper.id:
        return RedirectResponse("/me?error=not_your_assignment", status_code=303)

    # Gibt es bereits ein offenes Angebot für diese Zuweisung?
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

    offer = models.ShiftSwapOffer(
        assignment_id=assignment_id,
        offered_by_helper_id=helper.id,
        message=message,
        status="open",
    )
    db.add(offer)
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
        )
        .order_by(models.ShiftSwapOffer.created_at.desc())
        .all()
    )
    # Eigene Angebote vom Board trennen (nicht übernehmbar)
    foreign_offers = [o for o in offers if o.offered_by_helper_id != helper.id]
    own_offers = [o for o in offers if o.offered_by_helper_id == helper.id]

    # Für jedes Angebot prüfen, ob der aktuelle Helfer es übernehmen kann
    takeable_flags: dict[int, tuple[bool, str]] = {}
    avail_day_ids = {a.day_id for a in helper.availabilities}
    my_other_assignments = [a for a in helper.shift_assignments]
    for off in foreign_offers:
        takeable_flags[off.id] = _can_helper_take_shift(
            helper, off.assignment, avail_day_ids, my_other_assignments,
        )

    return templates.TemplateResponse(
        "helper_board.html",
        _ctx(
            request, helper,
            foreign_offers=foreign_offers,
            own_offers=own_offers,
            takeable_flags=takeable_flags,
        ),
    )


@router.post("/board/{offer_id}/take")
def board_take(offer_id: int, request: Request, db: Session = Depends(get_db)):
    redir, helper = require_helper_redirect(request, db)
    if redir:
        return redir

    offer = (
        db.query(models.ShiftSwapOffer)
        .filter(models.ShiftSwapOffer.id == offer_id)
        .options(
            joinedload(models.ShiftSwapOffer.assignment).joinedload(models.ShiftAssignment.shift),
        )
        .one_or_none()
    )
    if not offer or offer.status != "open":
        return RedirectResponse("/board?error=not_available", status_code=303)
    if offer.offered_by_helper_id == helper.id:
        return RedirectResponse("/board?error=own_offer", status_code=303)

    avail_day_ids = {a.day_id for a in helper.availabilities}
    can, reason = _can_helper_take_shift(
        helper, offer.assignment, avail_day_ids, list(helper.shift_assignments),
    )
    if not can:
        return RedirectResponse(f"/board?error={reason}", status_code=303)

    # Assignment transferieren; role_id bleibt unverändert.
    assignment = offer.assignment
    old_helper_id = assignment.helper_id
    assignment.helper_id = helper.id

    # Offer abschließen
    offer.status = "taken"
    offer.taken_by_helper_id = helper.id
    offer.resolved_at = datetime.utcnow()

    # Falls der Übernehmer den Tag noch nicht als verfügbar markiert hatte,
    # fügen wir ihn stillschweigend hinzu. Wer freiwillig eine Schicht am Tag
    # X übernimmt, IST an Tag X verfügbar.
    day_id = assignment.shift.day_id
    if day_id not in avail_day_ids:
        db.add(models.Availability(helper_id=helper.id, day_id=day_id))

    # Alle anderen offenen Requests auf diese Assignment abbrechen — der
    # ursprüngliche Besitzer gibt sie ja gerade weg.
    db.query(models.ShiftSwapRequest).filter(
        models.ShiftSwapRequest.from_assignment_id == assignment.id,
        models.ShiftSwapRequest.status == "pending",
    ).update({"status": "cancelled", "resolved_at": datetime.utcnow()})

    db.commit()

    # Optional: Mail an ursprünglichen Besitzer
    if settings.smtp_enabled:
        try:
            from ..email_sender import send_swap_taken_email
            old_helper = db.get(models.Helper, old_helper_id)
            if old_helper:
                send_swap_taken_email(old_helper, helper, assignment)
        except Exception as exc:  # noqa: BLE001
            print(f"[board_take] SMTP-Fehler: {exc}")

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
async def me_swap_submit(assignment_id: int, request: Request, db: Session = Depends(get_db)):
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
        try:
            from ..email_sender import send_swap_request_email
            send_swap_request_email(target, helper, assignment, message)
        except Exception as exc:  # noqa: BLE001
            print(f"[swap_request] SMTP-Fehler: {exc}")

    return RedirectResponse("/me?swap_sent=1", status_code=303)


@router.post("/me/swap-requests/{request_id}/accept")
def me_swap_accept(request_id: int, request: Request, db: Session = Depends(get_db)):
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
        try:
            from ..email_sender import send_swap_accepted_email
            requester = db.get(models.Helper, old_helper_id)
            if requester:
                send_swap_accepted_email(requester, helper, assignment)
        except Exception as exc:  # noqa: BLE001
            print(f"[swap_accept] SMTP-Fehler: {exc}")

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
