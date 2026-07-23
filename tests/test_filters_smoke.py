"""Smoke-Test fuer die Admin-Filter (Helferliste, Mail, CSV-Export).

Deckt zwei Dinge ab:
1. Regression: /admin/helpers, /admin/shifts, /admin/mail laden ohne 500er
   (das war genau das Symptom des "column does not exist"-Bugs).
2. Die neuen Filter aus diesem Ticket:
   - pfand_bezahlt=yes/no (Pfand-befreite zaehlen als "bezahlt")
   - segment=has_shifts ("min. eine Schicht")
   inkl. Kombination beider Filter (UND-verknuepft) und Konsistenz
   zwischen Helferliste, Mail-Seite und /export/emails.csv.

Laeuft komplett gegen eine frische SQLite-Datei (keine Postgres/Neon-
Verbindung noetig). Aufruf:

    pytest tests/test_filters_smoke.py -v
"""
from __future__ import annotations

from datetime import date, time

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.main import app


@pytest.fixture(scope="module")
def client():
    SessionLocal, teardown = make_isolated_session_factory()
    db = SessionLocal()
    try:
        day = models.FestivalDay(date=date(2026, 8, 14), label="Freitag", sort_order=0)
        area = models.Area(name="Bar", description="", sort_order=0)
        db.add_all([day, area])
        db.flush()

        def make_shift(label):
            s = models.Shift(area_id=area.id, day_id=day.id, label=label,
                              start_time=time(18, 0), end_time=time(22, 0), capacity=5)
            db.add(s)
            db.flush()
            return s

        # --- Vier Helfer:innen, die die relevanten Kombinationen abdecken ---
        anna = models.Helper(  # bezahlt, 0 Schichten
            first_name="Anna", last_name="Bezahlt", email="anna@example.org",
            date_of_birth=date(1995, 1, 1),
            pfand_paid=True, pfand_exempt=False,
        )
        ben = models.Helper(  # Pfand-befreit, 1 Schicht (unter Soll=2)
            first_name="Ben", last_name="Befreit", email="ben@example.org",
            date_of_birth=date(1996, 2, 2),
            pfand_paid=False, pfand_exempt=True,
        )
        clara = models.Helper(  # noch nicht bezahlt, 2 Schichten (erreicht Soll)
            first_name="Clara", last_name="Offen", email="clara@example.org",
            date_of_birth=date(1997, 3, 3),
            pfand_paid=False, pfand_exempt=False,
        )
        david = models.Helper(  # bezahlt+zurueck, Ein-Schicht-Ticket, 1 Schicht (erreicht Soll=1)
            first_name="David", last_name="Zurueck", email="david@example.org",
            date_of_birth=date(1998, 4, 4),
            pfand_paid=True, pfand_returned=True, pfand_exempt=False,
            wants_only_one_shift=True,
        )
        db.add_all([anna, ben, clara, david])
        db.flush()

        shifts = [make_shift(f"Schicht {i}") for i in range(3)]
        db.add(models.ShiftAssignment(shift_id=shifts[0].id, helper_id=ben.id))
        db.add(models.ShiftAssignment(shift_id=shifts[0].id, helper_id=clara.id))
        db.add(models.ShiftAssignment(shift_id=shifts[1].id, helper_id=clara.id))
        db.add(models.ShiftAssignment(shift_id=shifts[1].id, helper_id=david.id))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as c:
        resp = c.post("/admin/login", data={
            "username": "test-admin", "password": "test-pw-123",
        }, follow_redirects=False)
        assert resp.status_code in (302, 303), f"Login fehlgeschlagen: {resp.status_code}"
        yield c

    teardown()


def _names(html: str) -> set[str]:
    """Grobe Heuristik: welche Nachnamen aus unseren Fixtures tauchen im HTML auf."""
    return {n for n in ("Bezahlt", "Befreit", "Offen", "Zurueck") if n in html}


def _mail_page_emails(html: str) -> set[str]:
    """Die Mail-Seite listet nur Email-Adressen (Textarea), keine Namen."""
    return {e for e in ("anna@example.org", "ben@example.org",
                        "clara@example.org", "david@example.org") if e in html}


def _csv_emails(csv_text: str) -> set[str]:
    import csv
    import io
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    return {row["email"] for row in reader}


# ---------------------------------------------------------------------------
# 1) Regression: Kernseiten laden ohne 500er
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/admin/helpers", "/admin/shifts", "/admin/mail"])
def test_pages_load_without_error(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text[:300]}"


# ---------------------------------------------------------------------------
# 2) Pfand-Filter: Pfand-befreite zaehlen als "bezahlt"
# ---------------------------------------------------------------------------
def test_helpers_pfand_bezahlt_yes_includes_exempt(client):
    resp = client.get("/admin/helpers?pfand_bezahlt=yes")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Bezahlt", "Befreit", "Zurueck"}


def test_helpers_pfand_bezahlt_no_excludes_exempt(client):
    resp = client.get("/admin/helpers?pfand_bezahlt=no")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Offen"}


def test_mail_pfand_bezahlt_matches_helpers(client):
    resp = client.get("/admin/mail?pfand_bezahlt=yes")
    assert resp.status_code == 200
    assert _mail_page_emails(resp.text) == {
        "anna@example.org", "ben@example.org", "david@example.org",
    }


def test_export_emails_pfand_bezahlt_matches(client):
    resp = client.get("/admin/export/emails.csv?pfand_bezahlt=no")
    assert resp.status_code == 200
    assert _csv_emails(resp.text) == {"clara@example.org"}


# ---------------------------------------------------------------------------
# 3) Segment "min. eine Schicht" (has_shifts)
# ---------------------------------------------------------------------------
def test_helpers_segment_has_shifts(client):
    resp = client.get("/admin/helpers?segment=has_shifts")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Befreit", "Offen", "Zurueck"}


def test_helpers_segment_no_shifts_and_has_shifts_covers_everyone(client):
    # ODER-verknuepft -> Vereinigung sollte alle vier abdecken (Komplement-Check).
    resp = client.get("/admin/helpers?segment=no_shifts&segment=has_shifts")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Bezahlt", "Befreit", "Offen", "Zurueck"}


def test_helpers_segment_below_soll_only_ben(client):
    resp = client.get("/admin/helpers?segment=below_soll")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Befreit"}


def test_mail_segment_has_shifts_matches_helpers(client):
    resp = client.get("/admin/mail?segment=has_shifts")
    assert resp.status_code == 200
    assert _mail_page_emails(resp.text) == {
        "ben@example.org", "clara@example.org", "david@example.org",
    }


# ---------------------------------------------------------------------------
# 4) Kombination: has_shifts UND pfand_bezahlt=no -> nur Clara
# ---------------------------------------------------------------------------
def test_combined_filters_and_logic(client):
    resp = client.get("/admin/helpers?segment=has_shifts&pfand_bezahlt=no")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Offen"}
