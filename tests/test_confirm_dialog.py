"""Tests fuer den gemeinsamen Bestaetigungsdialog (_confirm_dialog.html).

Zwei Dinge werden abgesichert:
1. Der Dialog steckt in beiden Layouts (Admin und Helfer:innen-Bereich).
2. Es rutscht kein natives confirm() in die Templates zurueck.

Punkt 2 ist der eigentliche Wert: der Dialog ist leicht zu vergessen, wenn
spaeter mal schnell ein Loesch-Button dazukommt.

    pytest tests/test_confirm_dialog.py -v
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, time

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.main import app


TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"

# Der 75€-Dialog in admin/helper_detail.html hat eigene Sonderlogik (er darf nur
# erscheinen, wenn wirklich eine Mail rausgeht) und ist bewusst ausgenommen.
ALLOWED_CONFIRM_TOKENS = ("confirmDiscountSave", "discountDialogConfirmed")


@pytest.fixture(scope="module")
def client():
    SessionLocal, teardown = make_isolated_session_factory()
    db = SessionLocal()
    try:
        day = models.FestivalDay(date=date(2026, 8, 14), label="Freitag", sort_order=0)
        area = models.Area(name="Bar", description="", sort_order=0)
        db.add_all([day, area])
        db.flush()
        shift = models.Shift(area_id=area.id, day_id=day.id, label="Schicht A",
                             start_time=time(18, 0), end_time=time(22, 0), capacity=5)
        helper = models.Helper(
            first_name="Anna", last_name="Alpha", email="alpha@example.org",
            date_of_birth=date(1995, 1, 1), status="registered",
        )
        db.add_all([shift, helper])
        db.commit()
        db.refresh(shift)
        db.refresh(helper)
        ids = (shift.id, helper.id)
    finally:
        db.close()

    with TestClient(app) as c:
        resp = c.post("/admin/login", data={
            "username": "test-admin", "password": "test-pw-123",
        }, follow_redirects=False)
        assert resp.status_code in (302, 303), f"Login fehlgeschlagen: {resp.status_code}"
        c.test_ids = ids
        yield c

    teardown()


# ---------------------------------------------------------------------------
# 1) Keine nativen confirm()-Aufrufe mehr in den Templates
# ---------------------------------------------------------------------------
def test_no_native_confirm_left_in_templates():
    offenders = []
    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Kommentare zaehlen nicht.
            if "<!--" in line or line.strip().startswith("#"):
                continue
            if not re.search(r"\bconfirm\s*\(", line):
                continue
            if any(tok in line for tok in ALLOWED_CONFIRM_TOKENS):
                continue
            if path.name == "_confirm_dialog.html":
                continue
            offenders.append(f"{path.relative_to(TEMPLATE_DIR)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Natives confirm() gefunden - bitte auf data-confirm umstellen:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 2) Der Dialog wird in beiden Layouts ausgeliefert
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/admin", "/admin/config", "/login"])
def test_dialog_present_in_both_layouts(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    assert 'id="app-confirm-dialog"' in resp.text, f"Dialog fehlt in {path}"


# ---------------------------------------------------------------------------
# 3) Die umgestellten Formulare tragen data-confirm
# ---------------------------------------------------------------------------
def test_config_delete_forms_use_data_confirm(client):
    resp = client.get("/admin/config")
    assert resp.status_code == 200
    assert resp.text.count("data-confirm=") >= 2, "Tag- und Bereich-Löschen erwartet"
    assert "data-confirm-danger" in resp.text


def test_helper_detail_forms_use_data_confirm(client):
    _, helper_id = client.test_ids
    resp = client.get(f"/admin/helpers/{helper_id}")
    assert resp.status_code == 200
    # Löschen, Reset-Link, Verifikationsmail
    assert resp.text.count("data-confirm=") >= 3
    # Der 75€-Dialog bleibt daneben unangetastet.
    assert "confirmDiscountSave" in resp.text


def test_shift_detail_delete_uses_data_confirm(client):
    shift_id, _ = client.test_ids
    resp = client.get(f"/admin/shifts/{shift_id}")
    assert resp.status_code == 200
    assert "data-confirm=" in resp.text
    assert "data-confirm-danger" in resp.text
