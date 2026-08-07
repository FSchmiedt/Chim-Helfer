"""Direkt-Anmeldung: Schicht-zuerst-Flow.

Ein bewusst schlanker, dreistufiger Ablauf für Leute, die noch KEIN Konto
haben und sich in einem Rutsch direkt in eine (oder zwei) offene Schicht(en)
eintragen wollen:

  Schritt 1  offene Schicht(en) auswählen (1 oder 2; 1 = 75€-Ticket)
  Schritt 2  Name, Email, Handynummer, Geburtsdatum, (vorgeschlagenes) Passwort
  Schritt 3  Kautions-Frage (kann ich die 160€ zahlen? sonst: was geht?)

Danach wird die Person angelegt, in die gewählten Schichten eingetragen und
automatisch eingeloggt (Weiterleitung auf /me).

Absichtlich getrennt vom klassischen Formular (public.register_*) und vom
eingeloggten Self-Signup (helper_area./schichten). Der Schalter dafür ist
`DIRECT_SIGNUP_OPEN` / `DIRECT_SIGNUP_OPEN_AT` (siehe config.py), unabhängig
von `REGISTRATION_OPEN` und `SHIFT_SIGNUP_OPEN` ansteuerbar.

Die eigentliche "/"-Verzweigung sitzt in public.register_form und ruft von
hier `render_direct_page(...)` auf, wenn der Direkt-Flow offen ist.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..auth import HELPER_COOKIE_NAME, make_helper_session_cookie
from ..config import settings
from ..database import get_db
from ..passwords import generate_token, hash_password
from ..shift_log import log_shift_change


router = APIRouter(tags=["direct"])
templates = Jinja2Templates(directory="app/templates")

# Der Direkt-Flow fragt kein Geburtsdatum ab - das Alter wird am Eingang per
# Ausweis geprueft. Die Spalte helpers.date_of_birth ist aber NOT NULL, also
# schreiben wir denselben Platzhalter, den auch das Admin-Formular setzt, wenn
# dort nichts eingetragen wird. Achtung beim Auswerten: fuer alle ueber diesen
# Weg Angemeldeten steht im Admin der 01.01.1990, das ist kein echtes Datum.
PLACEHOLDER_DOB = date(1990, 1, 1)


# ---------------------------------------------------------------------------
# Passwort-Vorschlag: freundliche, leicht merkbare Kräuter/Gewürze + Zahl.
# ---------------------------------------------------------------------------
_FRIENDLY_WORDS = [
    "petersilie", "ingwer", "basilikum", "koriander", "kurkuma", "zimt",
    "kardamom", "majoran", "thymian", "rosmarin", "salbei", "minze",
    "lavendel", "holunder", "kamille", "melisse", "fenchel", "anis",
    "muskat", "vanille", "safran", "estragon", "kerbel", "wacholder",
    "sanddorn", "quitte", "mirabelle", "haselnuss", "walnuss", "pfirsich",
]


def suggest_password() -> str:
    """Erzeugt einen freundlichen Vorschlag wie 'ingwer-482'.

    Bewusst nicht hochsicher: unter /me stehen praktisch keine sensiblen
    Daten. Die Person kann ein eigenes wählen oder das Feld leer lassen.
    """
    word = secrets.choice(_FRIENDLY_WORDS)
    number = secrets.randbelow(900) + 100  # 100..999
    return f"{word}-{number}"


# ---------------------------------------------------------------------------
# Reservierungen ("Holds") — Kinoticket-Prinzip
# ---------------------------------------------------------------------------
def new_hold_token() -> str:
    """Anonymer Token pro Formular-Ladevorgang."""
    return secrets.token_urlsafe(24)


def cleanup_expired_holds(db: Session) -> None:
    """Abgelaufene Reservierungen entfernen (lazy, ohne Cronjob)."""
    db.query(models.ShiftHold).filter(
        models.ShiftHold.expires_at < datetime.utcnow()
    ).delete(synchronize_session=False)


def active_holds_by_shift(db: Session, exclude_token: Optional[str] = None) -> dict[int, int]:
    """Anzahl aktiver (nicht abgelaufener) fremder Reservierungen je Schicht.

    `exclude_token` blendet die eigenen Holds aus, damit man die von sich selbst
    gehaltene Schicht in der Liste nicht als "belegt" sieht.
    """
    q = db.query(models.ShiftHold).filter(
        models.ShiftHold.expires_at >= datetime.utcnow()
    )
    if exclude_token:
        q = q.filter(models.ShiftHold.token != exclude_token)
    counts: dict[int, int] = {}
    for h in q.all():
        counts[h.shift_id] = counts.get(h.shift_id, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Offene Schichten laden + gruppieren
# ---------------------------------------------------------------------------
def _load_open_shifts(db: Session, viewer_token: Optional[str] = None):
    """Liefert alle Schichten aus ALLEN Bereichen, die noch freie Plätze haben,
    gruppiert nach Tag -> Bereich, in stabiler Reihenfolge.

    Rückgabe: Liste von Tages-Blöcken:
      [{"day": FestivalDay, "areas": [{"area": Area, "shifts": [shift_info]}]}]
    shift_info: {"shift": Shift, "n_assigned": int, "free": int, "is_bar": bool}
    """
    shifts = (
        db.query(models.Shift)
        .options(
            joinedload(models.Shift.area),
            joinedload(models.Shift.day),
            joinedload(models.Shift.assignments),
        )
        .all()
    )

    # Abgelaufene Reservierungen wegräumen, dann aktive fremde Holds zählen.
    cleanup_expired_holds(db)
    held = active_holds_by_shift(db, exclude_token=viewer_token)

    # nur Schichten mit tatsächlich freier Kapazität (Belegung + fremde Holds)
    open_shifts = []
    for s in shifts:
        n_taken = len(s.assignments) + held.get(s.id, 0)
        if n_taken < s.capacity:
            open_shifts.append((s, n_taken))

    # Tage in Sortier-Reihenfolge
    days = (
        db.query(models.FestivalDay)
        .order_by(models.FestivalDay.sort_order, models.FestivalDay.date)
        .all()
    )
    day_order = {d.id: (d.sort_order, d.date) for d in days}

    # gruppieren
    by_day: dict[int, dict[int, list]] = {}
    day_by_id: dict[int, models.FestivalDay] = {d.id: d for d in days}
    area_by_id: dict[int, models.Area] = {}
    for s, n_assigned in open_shifts:
        by_day.setdefault(s.day_id, {}).setdefault(s.area_id, []).append((s, n_assigned))
        area_by_id[s.area_id] = s.area
        day_by_id.setdefault(s.day_id, s.day)

    blocks = []
    for day_id in sorted(by_day.keys(), key=lambda did: day_order.get(did, (99, date.max))):
        areas_dict = by_day[day_id]
        area_blocks = []
        for area_id in sorted(areas_dict.keys(),
                              key=lambda aid: (area_by_id[aid].sort_order, area_by_id[aid].name)):
            items = sorted(areas_dict[area_id], key=lambda t: t[0].start_time)
            area = area_by_id[area_id]
            is_bar = area.name.strip().lower() == "bar"
            shift_infos = [
                {
                    "shift": s,
                    "n_assigned": n,
                    "free": s.capacity - n,
                    "is_bar": is_bar,
                }
                for s, n in items
            ]
            area_blocks.append({"area": area, "is_bar": is_bar, "shifts": shift_infos})
        blocks.append({"day": day_by_id[day_id], "areas": area_blocks})

    return blocks


# ---------------------------------------------------------------------------
# Seite rendern (aus "/" via public.register_form ODER aus /vorschau)
# ---------------------------------------------------------------------------
def render_direct_page(
    request: Request,
    db: Session,
    *,
    preview: bool = False,
    preview_token: Optional[str] = None,
    errors: Optional[list[str]] = None,
    form_data: Optional[dict] = None,
    hold_token: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    # Token für Reservierungen: bei Fehler-Neuanzeige den bestehenden behalten,
    # sonst einen frischen erzeugen (die Person hat dann noch keine Holds).
    hold_token = hold_token or (form_data or {}).get("hold_token") or new_hold_token()
    blocks = _load_open_shifts(db, viewer_token=hold_token)
    total_open = sum(
        len(a["shifts"]) for b in blocks for a in b["areas"]
    )
    return templates.TemplateResponse(
        "direct_signup.html",
        {
            "request": request,
            "festival_name": settings.FESTIVAL_NAME,
            "blocks": blocks,
            "total_open": total_open,
            "preview": preview,
            "preview_token": preview_token or "",
            "errors": errors,
            "form_data": form_data or {},
            "hold_token": hold_token,
            "suggested_password": (form_data or {}).get("password") or suggest_password(),
            "deposit_amount": settings.DEPOSIT_AMOUNT_EUR,
            "deposit_partial": settings.DEPOSIT_PARTIAL_EUR,
            "one_shift_price": settings.ONE_SHIFT_PRICE_EUR,
            "bar_hint": settings.DIRECT_SIGNUP_BAR_HINT,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Interne Vorschau (geheime URL): /vorschau?key=<TOKEN>
# ---------------------------------------------------------------------------
@router.get("/vorschau", response_class=HTMLResponse)
def direct_preview(request: Request, db: Session = Depends(get_db)):
    token = (settings.DIRECT_SIGNUP_PREVIEW_TOKEN or "").strip()
    key = (request.query_params.get("key") or "").strip()
    # Kein Token gesetzt ODER falscher Key -> so tun, als gäbe es die Seite nicht.
    if not token or not key or not secrets.compare_digest(key, token):
        return templates.TemplateResponse(
            "direct_preview_locked.html",
            {"request": request, "festival_name": settings.FESTIVAL_NAME},
            status_code=404,
        )
    return render_direct_page(request, db, preview=True, preview_token=token)


# ---------------------------------------------------------------------------
# Reservierung setzen: /mitmachen/hold (JSON, aus dem Frontend beim Schritt 1->2)
# ---------------------------------------------------------------------------
@router.post("/mitmachen/hold")
async def direct_hold(request: Request, db: Session = Depends(get_db)):
    """Reserviert die gewählten Schichten für einen Formular-Token.

    Wird per fetch() aufgerufen, wenn die Person von Schritt 1 (Schicht wählen)
    zu Schritt 2 (Kontakt) weitergeht. Antwortet mit JSON:
      { "ok": true }                      -> alle Wünsche reserviert, weiter
      { "ok": false, "unavailable": [ {id, label}, ... ] }
                                          -> mind. eine ist weg, Person bleibt

    Setzt den Hold-Zustand für den Token auf GENAU die aktuell gewählten
    Schichten (frühere Holds desselben Tokens für andere Schichten werden
    freigegeben — z.B. wenn jemand zurückgeht und die Auswahl ändert).
    """
    from fastapi.responses import JSONResponse

    # Zugang wie beim finalen Absenden: offen ODER gültige Vorschau.
    form = await request.form()
    submitted_token = (form.get("preview_token") or "").strip()
    cfg_token = (settings.DIRECT_SIGNUP_PREVIEW_TOKEN or "").strip()
    is_preview = bool(cfg_token) and bool(submitted_token) and secrets.compare_digest(submitted_token, cfg_token)
    if not settings.direct_signup_effective_open and not is_preview:
        return JSONResponse({"ok": False, "error": "closed"}, status_code=403)

    hold_token = (form.get("hold_token") or "").strip()
    if not hold_token:
        return JSONResponse({"ok": False, "error": "no_token"}, status_code=400)

    try:
        wanted_ids = list(dict.fromkeys(int(x) for x in form.getlist("shift_ids")))
    except ValueError:
        wanted_ids = []
    if not wanted_ids:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    cleanup_expired_holds(db)

    # Bestehende Holds dieses Tokens, die nicht mehr gewünscht sind, freigeben.
    db.query(models.ShiftHold).filter(
        models.ShiftHold.token == hold_token,
        ~models.ShiftHold.shift_id.in_(wanted_ids),
    ).delete(synchronize_session=False)

    shifts = (
        db.query(models.Shift)
        .filter(models.Shift.id.in_(wanted_ids))
        .options(joinedload(models.Shift.area), joinedload(models.Shift.day))
        .all()
    )
    shift_by_id = {s.id: s for s in shifts}

    held_by_others = active_holds_by_shift(db, exclude_token=hold_token)
    my_holds = {
        h.shift_id
        for h in db.query(models.ShiftHold).filter(
            models.ShiftHold.token == hold_token,
            models.ShiftHold.expires_at >= datetime.utcnow(),
        ).all()
    }

    expires = datetime.utcnow() + timedelta(minutes=settings.DIRECT_SIGNUP_HOLD_MINUTES)
    unavailable = []
    for sid in wanted_ids:
        s = shift_by_id.get(sid)
        if s is None:
            unavailable.append({"id": sid, "label": "Diese Schicht"})
            continue
        n_assigned = db.query(models.ShiftAssignment).filter(
            models.ShiftAssignment.shift_id == sid
        ).count()
        taken = n_assigned + held_by_others.get(sid, 0)
        already_mine = sid in my_holds
        # Platz frei ODER ich halte sie eh schon -> (re-)servieren.
        if taken < s.capacity or already_mine:
            if already_mine:
                db.query(models.ShiftHold).filter(
                    models.ShiftHold.token == hold_token,
                    models.ShiftHold.shift_id == sid,
                ).update({"expires_at": expires}, synchronize_session=False)
            else:
                db.add(models.ShiftHold(shift_id=sid, token=hold_token, expires_at=expires))
        else:
            label = f"{s.area.name} · {s.day.label} · {s.time_range}"
            unavailable.append({"id": sid, "label": label})

    if unavailable:
        # Nichts committen, wenn eine weg ist: die Person bleibt in Schritt 1,
        # ändert die Auswahl und versucht es erneut.
        db.rollback()
        return JSONResponse({"ok": False, "unavailable": unavailable})

    db.commit()
    return JSONResponse({"ok": True, "expires_in_minutes": settings.DIRECT_SIGNUP_HOLD_MINUTES})


# ---------------------------------------------------------------------------
# Absenden: /mitmachen (POST)
# ---------------------------------------------------------------------------
@router.post("/mitmachen", response_class=HTMLResponse)
async def direct_submit(request: Request, background_tasks: BackgroundTasks,
                        db: Session = Depends(get_db)):
    form = await request.form()

    # Vorschau-Modus? Nur mit korrektem Token aus dem Formular.
    submitted_token = (form.get("preview_token") or "").strip()
    cfg_token = (settings.DIRECT_SIGNUP_PREVIEW_TOKEN or "").strip()
    is_preview = bool(cfg_token) and bool(submitted_token) and secrets.compare_digest(submitted_token, cfg_token)

    # Zugang: offen ODER gültige Vorschau.
    if not settings.direct_signup_effective_open and not is_preview:
        return templates.TemplateResponse(
            "registration_closed.html",
            {
                "request": request,
                "festival_name": settings.FESTIVAL_NAME,
                "custom_message": settings.REGISTRATION_CLOSED_MESSAGE or None,
            },
            status_code=403,
        )

    # --- Eingaben einlesen ---
    shift_ids_raw = form.getlist("shift_ids")
    try:
        shift_ids = [int(x) for x in shift_ids_raw]
    except ValueError:
        shift_ids = []
    # Duplikate entfernen, Reihenfolge egal
    shift_ids = list(dict.fromkeys(shift_ids))

    only_one = form.get("only_one_shift") == "on"
    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    phone = (form.get("phone") or "").strip() or None
    password = form.get("password") or ""  # kann leer bleiben
    deposit_ok = form.get("deposit_ok")  # "yes" | "no" | None
    deposit_alt = (form.get("deposit_alternative") or "").strip()
    hold_token = (form.get("hold_token") or "").strip()

    form_echo = {
        "first_name": first_name, "last_name": last_name, "email": email,
        "phone": phone or "", "password": password,
        "only_one_shift": only_one, "shift_ids": shift_ids,
        "deposit_ok": deposit_ok, "deposit_alternative": deposit_alt,
        "hold_token": hold_token,
    }

    def fail(msgs, code=400):
        return render_direct_page(
            request, db,
            preview=is_preview,
            preview_token=cfg_token if is_preview else None,
            errors=msgs if isinstance(msgs, list) else [msgs],
            form_data=form_echo,
            hold_token=hold_token or None,
            status_code=code,
        )

    # --- Validierung: Schichten ---
    max_allowed = 1 if only_one else 2
    if not shift_ids:
        return fail("Bitte wähle mindestens eine Schicht aus.")
    if len(shift_ids) > max_allowed:
        if only_one:
            return fail("Du hast „nur eine Schicht“ gewählt - bitte genau eine Schicht auswählen.")
        return fail("Bitte wähle höchstens zwei Schichten aus.")

    # --- Validierung: Personendaten ---
    errs: list[str] = []
    if not first_name:
        errs.append("Bitte gib deinen Vornamen an.")
    if not last_name:
        errs.append("Bitte gib deinen Nachnamen an.")
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        errs.append("Bitte gib eine gültige Email-Adresse an.")
    # Kein Geburtsdatum, keine Volljaehrigkeits-Checkbox: das Alter wird beim
    # Einlass am Ausweis geprueft, nicht im Formular.
    #
    # Kautions-Frage: "yes" (voll), "partial" (Teilbetrag), "no" (gar nicht,
    # dafuer mit Begruendung).
    if deposit_ok not in ("yes", "partial", "no"):
        errs.append("Bitte beantworte die Frage zur Kaution.")
    elif deposit_ok == "no":
        if only_one:
            # Das 75-Euro-Ticket IST bereits der verguenstigte Weg - im Formular
            # ist die Option dann ausgeblendet. Hier nur der Server-Riegel, falls
            # jemand sie trotzdem mitschickt.
            errs.append(
                "Beim Ein-Schicht-Ticket brauchen wir eine Kaution. Bitte wähle, "
                "welchen Betrag du zahlen kannst."
            )
        elif not deposit_alt:
            errs.append("Bitte schreib uns kurz, warum wir uns auf dich verlassen können.")
    if errs:
        return fail(errs)

    # --- Doppel-Anmeldung? -> Fehlermeldung, bitte einloggen ---
    existing = db.query(models.Helper).filter(models.Helper.email == email).one_or_none()
    if existing:
        return fail([
            "Mit dieser Email gibt es schon ein Konto. Bitte log dich ein und "
            "trag dich dort unter „Freie Schichten“ ein - oder nutze „Passwort "
            "zurücksetzen“, falls du dein Passwort nicht mehr weißt."
        ])

    # --- Schichten laden + prüfen (existieren, frei, kein Zeitkonflikt) ---
    shifts = (
        db.query(models.Shift)
        .filter(models.Shift.id.in_(shift_ids))
        .options(joinedload(models.Shift.area), joinedload(models.Shift.day))
        .all()
    )
    found_ids = {s.id for s in shifts}
    missing = [sid for sid in shift_ids if sid not in found_ids]
    if missing:
        return fail("Eine der gewählten Schichten gibt es nicht mehr. Bitte lade die Seite neu und wähle erneut.")

    # Zeitkonflikt der gewählten Schichten untereinander (gleicher Tag, Overlap)
    for i in range(len(shifts)):
        for j in range(i + 1, len(shifts)):
            a, b = shifts[i], shifts[j]
            if a.day_id == b.day_id and a.start_time < b.end_time and b.start_time < a.end_time:
                return fail("Zwei deiner gewählten Schichten überschneiden sich zeitlich. Bitte wähle andere.")

    # --- Person anlegen ---
    notes = _deposit_note(deposit_ok, deposit_alt, settings.DEPOSIT_AMOUNT_EUR)
    helper = models.Helper(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        # Der Direkt-Flow fragt kein Geburtsdatum ab (Ausweiskontrolle am
        # Eingang). Die Spalte ist aber NOT NULL, also derselbe Platzhalter,
        # den auch das Admin-Formular setzt, wenn nichts eingetragen wurde.
        date_of_birth=PLACEHOLDER_DOB,
        notes=notes,
        is_adult_confirmed=True,
        accepted_no_guarantee=True,  # direkte Buchung = bewusste, verbindliche Zusage
        status="registered",
        wants_only_one_shift=only_one,
        password_hash=hash_password(password) if password.strip() else None,
        email_verification_token=generate_token(),
    )
    # 75€-Ticket-Kennzeichnung mitziehen (Badge/Hinweise hängen an discount_offered)
    if only_one:
        helper.discount_offered = True
        helper.discount_offered_at = datetime.utcnow()
    db.add(helper)
    db.flush()

    # --- Schichten buchen (mit Kapazitäts-Race-Schutz je Schicht) ---
    try:
        booked_days: set[int] = set()
        for s in shifts:
            n_before = db.query(models.ShiftAssignment).filter(
                models.ShiftAssignment.shift_id == s.id
            ).count()
            if n_before >= s.capacity:
                db.rollback()
                label = f"{s.area.name} · {s.day.label} · {s.time_range}"
                return fail(
                    f"Die Schicht „{label}“ wurde gerade voll. "
                    "Bitte lade die Seite neu und wähle eine andere."
                )
            db.add(models.ShiftAssignment(shift_id=s.id, helper_id=helper.id, role_id=None))
            db.flush()
            n_after = db.query(models.ShiftAssignment).filter(
                models.ShiftAssignment.shift_id == s.id
            ).count()
            if n_after > s.capacity:
                db.rollback()
                label = f"{s.area.name} · {s.day.label} · {s.time_range}"
                return fail(
                    f"Die Schicht „{label}“ wurde gerade von jemand anderem "
                    "genommen. Bitte lade die Seite neu."
                )
            booked_days.add(s.day_id)

        # Verfügbarkeit für die betroffenen Tage nachziehen
        for day_id in booked_days:
            db.add(models.Availability(helper_id=helper.id, day_id=day_id))

        # Protokoll je Schicht
        for s in shifts:
            log_shift_change(
                db, helper_id=helper.id, shift=s,
                action="assigned", source="self_signup",
            )

        # Eigene Reservierungen sind erfüllt -> freigeben. Abgelaufene fremde
        # gleich mit wegräumen (lazy cleanup).
        if hold_token:
            db.query(models.ShiftHold).filter(
                models.ShiftHold.token == hold_token
            ).delete(synchronize_session=False)
        cleanup_expired_holds(db)

        db.commit()
    except Exception:
        db.rollback()
        return fail(
            "Beim Eintragen ist etwas schiefgelaufen. Bitte lade die Seite neu und "
            "versuch es noch einmal.",
            code=500,
        )

    # --- Verifikations-Mail (falls SMTP) ---
    base = str(request.base_url).rstrip("/")
    verify_url = f"{base}/verify/{helper.email_verification_token}"
    if settings.smtp_enabled:
        from ..email_sender import build_verification_message, deliver, send_in_background
        msg = build_verification_message(helper, verify_url)
        send_in_background(background_tasks, deliver, msg, label="direct_register")
    else:
        print(f"[direct] Verifikations-Link für {helper.email}: {verify_url}")

    # --- Auto-Login + Weiterleitung auf /me ---
    resp = RedirectResponse("/me?welcome=1", status_code=303)
    resp.set_cookie(
        HELPER_COOKIE_NAME,
        make_helper_session_cookie(helper.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        secure=False,
    )
    return resp


def _deposit_note(deposit_ok: Optional[str], deposit_alt: str, amount: int) -> str:
    """Baut die Kautions-Selbstauskunft als Notiz (im Admin sichtbar)."""
    if deposit_ok == "yes":
        return f"[Direkt-Anmeldung] Kaution ({amount} €): kann ich zahlen."
    if deposit_ok == "partial":
        return (f"[Direkt-Anmeldung] Kaution ({amount} €): kann ich nicht voll zahlen, "
                f"aber {settings.DEPOSIT_PARTIAL_EUR} €.")
    if deposit_ok == "no":
        if deposit_alt:
            return (f"[Direkt-Anmeldung] Kaution ({amount} €): gar nicht möglich. "
                    f"Begründung: {deposit_alt}")
        return f"[Direkt-Anmeldung] Kaution ({amount} €): gar nicht möglich (keine Begründung angegeben)."
    return "[Direkt-Anmeldung] Kaution: keine Angabe."
