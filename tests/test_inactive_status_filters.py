"""Tests fuer den Ausschluss inaktiver Helfer:innen (declined / withdrawn).

Wer abgesagt oder zurueckgezogen hat, ist aus der Planung raus - das Soll ist
faktisch 0. Solche Leute duerfen die "was ist noch offen"-Ansichten nicht
aufblaehen. Abgedeckt wird:

  - Dashboard-Kennzahl "Nicht verplant"
  - Segment "ohne Schichten" und "unter Soll"
  - Pfand-Filter "noch nicht bezahlt"
  - Mail-Verteiler und CSV-Export ohne expliziten Status-Filter

Bewusste Gegenproben (das soll NICHT gefiltert werden):
  - Segment "min. eine Schicht": wer zurueckgezogen hat und noch auf einer
    Schicht steht, muss sichtbar bleiben - sonst plant man ihn nie um.
  - Pfand-Filter "bezahlt": wer gezahlt hat, kriegt sein Pfand auch nach
    einer Absage zurueck.
  - Expliziter Status-Filter (?status=withdrawn) sticht in Mail und Export.

Eigene, isolierte SQLite-Datei (siehe tests/_dbutil.py). Aufruf:

    pytest tests/test_inactive_status_filters.py -v
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, time

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.main import app


# Neutrale Nachnamen: die Templates rendern in jedem Status-Dropdown die Woerter
# "abgelehnt" und "zurueckgezogen" mit. Sprechende Namen wuerden beim
# Substring-Matching unten false positives erzeugen.
ALL_SURNAMES = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")
ALL_EMAILS = (
    "alpha@example.org",
    "beta@example.org",
    "gamma@example.org",
    "delta@example.org",
    "epsilon@example.org",
)


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

        # MIN_SHIFTS ist im conftest auf 2 gesetzt -> "unter Soll" = genau 1 Schicht.
        alpha = models.Helper(  # aktiv, 0 Schichten, Pfand offen
            first_name="Anna", last_name="Alpha", email="alpha@example.org",
            date_of_birth=date(1995, 1, 1), status="registered",
            pfand_paid=False, pfand_exempt=False,
        )
        beta = models.Helper(  # aktiv, 1 Schicht (unter Soll), Pfand offen
            first_name="Bea", last_name="Beta", email="beta@example.org",
            date_of_birth=date(1996, 2, 2), status="confirmed",
            pfand_paid=False, pfand_exempt=False,
        )
        gamma = models.Helper(  # zurueckgezogen, 0 Schichten, Pfand offen
            first_name="Carl", last_name="Gamma", email="gamma@example.org",
            date_of_birth=date(1997, 3, 3), status="withdrawn",
            pfand_paid=False, pfand_exempt=False,
        )
        delta = models.Helper(  # abgelehnt, 0 Schichten, Pfand offen
            first_name="Dora", last_name="Delta", email="delta@example.org",
            date_of_birth=date(1998, 4, 4), status="declined",
            pfand_paid=False, pfand_exempt=False,
        )
        epsilon = models.Helper(  # zurueckgezogen, aber 1 Schicht + Pfand bezahlt
            first_name="Emil", last_name="Epsilon", email="epsilon@example.org",
            date_of_birth=date(1999, 5, 5), status="withdrawn",
            pfand_paid=True, pfand_exempt=False,
        )
        db.add_all([alpha, beta, gamma, delta, epsilon])
        db.flush()

        shift = make_shift("Schicht A")
        db.add(models.ShiftAssignment(shift_id=shift.id, helper_id=beta.id))
        db.add(models.ShiftAssignment(shift_id=shift.id, helper_id=epsilon.id))
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
    return {n for n in ALL_SURNAMES if n in html}


def _mail_emails(html: str) -> set[str]:
    return {e for e in ALL_EMAILS if e in html}


def _csv_emails(csv_text: str) -> set[str]:
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    return {row["email"] for row in reader}


def _unassigned_count(html: str) -> int:
    """Liest die Kennzahl aus der "Nicht verplant"-Kachel des Dashboards."""
    match = re.search(
        r"Nicht verplant.*?text-3xl[^>]*>\s*(\d+)\s*<", html, re.DOTALL
    )
    assert match, "Kachel 'Nicht verplant' nicht im Dashboard gefunden"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# 1) Dashboard: "Nicht verplant" zaehlt nur noch Aktive
# ---------------------------------------------------------------------------
def test_dashboard_unassigned_excludes_inactive(client):
    resp = client.get("/admin")
    assert resp.status_code == 200
    # Ohne Schicht sind Alpha, Gamma, Delta - aber nur Alpha ist noch dabei.
    assert _unassigned_count(resp.text) == 1


# ---------------------------------------------------------------------------
# 2) Segment-Filter
# ---------------------------------------------------------------------------
def test_segment_no_shifts_excludes_inactive(client):
    resp = client.get("/admin/helpers?segment=no_shifts")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Alpha"}


def test_segment_below_soll_excludes_inactive(client):
    # Beta und Epsilon haben beide genau 1 von 2 Schichten - Epsilon ist aber raus.
    resp = client.get("/admin/helpers?segment=below_soll")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Beta"}


def test_segment_has_shifts_keeps_inactive(client):
    # Gegenprobe: wer zurueckgezogen hat und noch eingeteilt ist, muss sichtbar
    # bleiben, damit die Schicht neu besetzt werden kann.
    resp = client.get("/admin/helpers?segment=has_shifts")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Beta", "Epsilon"}


# ---------------------------------------------------------------------------
# 3) Pfand-Filter
# ---------------------------------------------------------------------------
def test_pfand_bezahlt_no_excludes_inactive(client):
    resp = client.get("/admin/helpers?pfand_bezahlt=no")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Alpha", "Beta"}


def test_pfand_bezahlt_yes_keeps_inactive(client):
    # Gegenprobe: Epsilon hat gezahlt und bekommt sein Pfand trotz Absage zurueck.
    resp = client.get("/admin/helpers?pfand_bezahlt=yes")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Epsilon"}


def test_granular_pfand_dropdown_untouched(client):
    # Das granulare Dropdown (unpaid/paid/returned) bleibt bewusst ungefiltert:
    # es dient der Abwicklung vor Ort, nicht der Planung.
    resp = client.get("/admin/helpers?pfand=unpaid")
    assert resp.status_code == 200
    assert _names(resp.text) == {"Alpha", "Beta", "Gamma", "Delta"}


# ---------------------------------------------------------------------------
# 4) Mail-Verteiler und CSV-Export
# ---------------------------------------------------------------------------
def test_mail_without_filter_excludes_inactive(client):
    resp = client.get("/admin/mail")
    assert resp.status_code == 200
    assert _mail_emails(resp.text) == {"alpha@example.org", "beta@example.org"}


def test_mail_explicit_status_filter_wins(client):
    # Gezielt an die Zurueckgezogenen schreiben muss weiter moeglich sein.
    resp = client.get("/admin/mail?status=withdrawn")
    assert resp.status_code == 200
    assert _mail_emails(resp.text) == {"gamma@example.org", "epsilon@example.org"}


def test_export_without_filter_excludes_inactive(client):
    resp = client.get("/admin/export/emails.csv")
    assert resp.status_code == 200
    assert _csv_emails(resp.text) == {"alpha@example.org", "beta@example.org"}


def test_export_explicit_status_filter_wins(client):
    resp = client.get("/admin/export/emails.csv?status=declined")
    assert resp.status_code == 200
    assert _csv_emails(resp.text) == {"delta@example.org"}


# ---------------------------------------------------------------------------
# 5) Dokumentiert die bewusste Entscheidung aus der Abstimmung:
#    Status-Filter + "ohne Schichten" ergibt eine leere Liste. Kein Sonderfall-
#    Code - die Kombination ist in der Praxis sinnlos, der Test haelt nur fest,
#    dass es Absicht ist und nicht versehentlich kippt.
# ---------------------------------------------------------------------------
def test_status_withdrawn_plus_no_shifts_is_empty(client):
    resp = client.get("/admin/helpers?status=withdrawn&segment=no_shifts")
    assert resp.status_code == 200
    assert _names(resp.text) == set()
