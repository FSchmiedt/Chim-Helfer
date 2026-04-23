"""CSV-Import und -Export für Helfer-Daten."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Iterable

from sqlalchemy.orm import Session

from . import models


HELPER_CSV_COLUMNS = [
    "id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "date_of_birth",
    "iban",
    "paypal",
    "been_here_before",
    "previous_festivals",
    "availability_days",
    "preferred_areas",
    "notes",
    "admin_notes",
    "status",
    "pfand_paid",
    "pfand_paid_at",
    "pfand_returned",
    "pfand_returned_at",
    "created_at",
]


def helpers_to_csv(helpers: Iterable[models.Helper]) -> str:
    """Exportiert Helfer-Liste als CSV-String (mit Header)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HELPER_CSV_COLUMNS, delimiter=";")
    writer.writeheader()
    for h in helpers:
        # Verfügbarkeit als "Fr|Sa|So"
        days = "|".join(sorted(a.day.label for a in h.availabilities))
        # Wunschbereiche mit Rang: "1:Bar|2:Einlass"
        prefs = sorted(h.preferences, key=lambda p: p.rank)
        pref_str = "|".join(f"{p.rank}:{p.area.name}" for p in prefs)
        writer.writerow({
            "id": h.id,
            "first_name": h.first_name,
            "last_name": h.last_name,
            "email": h.email,
            "phone": h.phone or "",
            "date_of_birth": h.date_of_birth.isoformat(),
            "iban": h.iban or "",
            "paypal": h.paypal or "",
            "been_here_before": "ja" if h.been_here_before else "nein",
            "previous_festivals": h.previous_festivals or "",
            "availability_days": days,
            "preferred_areas": pref_str,
            "notes": (h.notes or "").replace("\n", " "),
            "admin_notes": (h.admin_notes or "").replace("\n", " "),
            "status": h.status,
            "pfand_paid": "ja" if h.pfand_paid else "nein",
            "pfand_paid_at": h.pfand_paid_at.isoformat(timespec="seconds") if h.pfand_paid_at else "",
            "pfand_returned": "ja" if h.pfand_returned else "nein",
            "pfand_returned_at": h.pfand_returned_at.isoformat(timespec="seconds") if h.pfand_returned_at else "",
            "created_at": h.created_at.isoformat(timespec="seconds") if h.created_at else "",
        })
    return buf.getvalue()


def emails_to_csv(helpers: Iterable[models.Helper]) -> str:
    """Minimale Email-Liste für Versand."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["email", "first_name", "last_name"])
    for h in helpers:
        writer.writerow([h.email, h.first_name, h.last_name])
    return buf.getvalue()


def import_helpers_from_csv(db: Session, csv_text: str) -> dict:
    """Einfacher Import: erzeugt oder aktualisiert Helfer anhand von Email.

    Erwartet die Spalten von HELPER_CSV_COLUMNS (id, created_at optional).
    """
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    created = 0
    updated = 0
    errors: list[str] = []

    # Alle Bereiche und Tage einmal vorladen
    areas = {a.name: a for a in db.query(models.Area).all()}
    days = {d.label: d for d in db.query(models.FestivalDay).all()}

    for line_num, row in enumerate(reader, start=2):
        email = (row.get("email") or "").strip().lower()
        if not email:
            errors.append(f"Zeile {line_num}: keine Email")
            continue

        helper = db.query(models.Helper).filter(models.Helper.email == email).one_or_none()
        is_new = helper is None

        try:
            dob_raw = (row.get("date_of_birth") or "").strip()
            dob = date.fromisoformat(dob_raw) if dob_raw else date(1990, 1, 1)
        except ValueError:
            errors.append(f"Zeile {line_num}: ungültiges Geburtsdatum '{dob_raw}'")
            continue

        if is_new:
            helper = models.Helper(
                email=email,
                first_name=(row.get("first_name") or "").strip() or "?",
                last_name=(row.get("last_name") or "").strip() or "?",
                date_of_birth=dob,
                is_adult_confirmed=True,
                accepted_no_guarantee=True,
            )
            db.add(helper)

        # Felder updaten
        helper.first_name = (row.get("first_name") or helper.first_name).strip()
        helper.last_name = (row.get("last_name") or helper.last_name).strip()
        helper.phone = (row.get("phone") or "").strip() or None
        helper.iban = (row.get("iban") or "").strip() or None
        helper.paypal = (row.get("paypal") or "").strip() or None
        helper.been_here_before = (row.get("been_here_before") or "").strip().lower() in ("ja", "yes", "true", "1")
        helper.previous_festivals = (row.get("previous_festivals") or "").strip() or None
        helper.notes = (row.get("notes") or "").strip() or None
        helper.admin_notes = (row.get("admin_notes") or "").strip() or None
        helper.status = (row.get("status") or "registered").strip() or "registered"
        helper.date_of_birth = dob

        # Pfand (optional — alte CSVs ohne die Spalten bleiben wie sie sind)
        if "pfand_paid" in row and row["pfand_paid"] is not None:
            helper.pfand_paid = (row.get("pfand_paid") or "").strip().lower() in ("ja", "yes", "true", "1")
            paid_at_raw = (row.get("pfand_paid_at") or "").strip()
            if paid_at_raw:
                try:
                    helper.pfand_paid_at = datetime.fromisoformat(paid_at_raw)
                except ValueError:
                    pass
        if "pfand_returned" in row and row["pfand_returned"] is not None:
            helper.pfand_returned = (row.get("pfand_returned") or "").strip().lower() in ("ja", "yes", "true", "1")
            ret_at_raw = (row.get("pfand_returned_at") or "").strip()
            if ret_at_raw:
                try:
                    helper.pfand_returned_at = datetime.fromisoformat(ret_at_raw)
                except ValueError:
                    pass

        db.flush()

        # Verfügbarkeit (wenn Spalte vorhanden)
        days_str = (row.get("availability_days") or "").strip()
        if days_str:
            # alte löschen
            db.query(models.Availability).filter(models.Availability.helper_id == helper.id).delete()
            for lbl in [x.strip() for x in days_str.split("|") if x.strip()]:
                day = days.get(lbl)
                if day:
                    db.add(models.Availability(helper_id=helper.id, day_id=day.id))

        # Wunschbereiche
        pref_str = (row.get("preferred_areas") or "").strip()
        if pref_str:
            db.query(models.HelperAreaPreference).filter(models.HelperAreaPreference.helper_id == helper.id).delete()
            for token in [x.strip() for x in pref_str.split("|") if x.strip()]:
                # Format "rank:Areaname" oder einfach "Areaname"
                if ":" in token:
                    rank_s, name = token.split(":", 1)
                    try:
                        rank = int(rank_s)
                    except ValueError:
                        rank = 1
                else:
                    rank = 1
                    name = token
                area = areas.get(name.strip())
                if area:
                    db.add(models.HelperAreaPreference(helper_id=helper.id, area_id=area.id, rank=rank))

        if is_new:
            created += 1
        else:
            updated += 1

    db.commit()
    return {"created": created, "updated": updated, "errors": errors}
