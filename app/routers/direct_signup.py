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
from datetime import date, datetime
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
# Offene Schichten laden + gruppieren
# ---------------------------------------------------------------------------
def _load_open_shifts(db: Session):
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

    # nur Schichten mit freier Kapazität
    open_shifts = []
    for s in shifts:
        n_assigned = len(s.assignments)
        if n_assigned < s.capacity:
            open_shifts.append((s, n_assigned))

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
    status_code: int = 200,
) -> HTMLResponse:
    blocks = _load_open_shifts(db)
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
            "suggested_password": (form_data or {}).get("password") or suggest_password(),
            "deposit_amount": settings.DEPOSIT_AMOUNT_EUR,
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
    dob_raw = (form.get("date_of_birth") or "").strip()
    password = form.get("password") or ""  # kann leer bleiben
    deposit_ok = form.get("deposit_ok")  # "yes" | "no" | None
    deposit_alt = (form.get("deposit_alternative") or "").strip()
    is_adult = form.get("is_adult_confirmed") == "on"

    form_echo = {
        "first_name": first_name, "last_name": last_name, "email": email,
        "phone": phone or "", "date_of_birth": dob_raw, "password": password,
        "only_one_shift": only_one, "shift_ids": shift_ids,
        "deposit_ok": deposit_ok, "deposit_alternative": deposit_alt,
        "is_adult_confirmed": is_adult,
    }

    def fail(msgs, code=400):
        return render_direct_page(
            request, db,
            preview=is_preview,
            preview_token=cfg_token if is_preview else None,
            errors=msgs if isinstance(msgs, list) else [msgs],
            form_data=form_echo,
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
    # Geburtsdatum + Volljährigkeit
    dob: Optional[date] = None
    if not dob_raw:
        errs.append("Bitte gib dein Geburtsdatum an.")
    else:
        try:
            dob = date.fromisoformat(dob_raw)
        except ValueError:
            errs.append("Bitte gib ein gültiges Geburtsdatum an (JJJJ-MM-TT).")
    if dob is not None:
        today = date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 18:
            errs.append("Du musst mindestens 18 Jahre alt sein.")
        elif age > 100:
            errs.append("Das Geburtsdatum scheint nicht zu stimmen.")
    if not is_adult:
        errs.append("Bitte bestätige, dass du volljährig bist.")
    # Kautions-Frage
    if deposit_ok not in ("yes", "no"):
        errs.append("Bitte beantworte die Frage zur Kaution.")
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
        date_of_birth=dob,
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
    if deposit_ok == "no":
        if deposit_alt:
            return (f"[Direkt-Anmeldung] Kaution ({amount} €): kann ich NICHT voll zahlen. "
                    f"Angebot: {deposit_alt}")
        return f"[Direkt-Anmeldung] Kaution ({amount} €): kann ich NICHT voll zahlen (kein Betrag angegeben)."
    return "[Direkt-Anmeldung] Kaution: keine Angabe."
