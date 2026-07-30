"""Tests fuer die angekuendigte Pfand-Zahlung und die Pfand-Anzeige auf /me.

Abgedeckt:
  - Filter-Option "bezahlt oder demnaechst" (paid ODER announced, OHNE exempt)
  - Speicherlogik: pfand_paid setzen loescht die Ankuendigung serverseitig
  - Frist wird gespeichert und beim Abwaehlen wieder geleert
  - /me zeigt je nach Pfand-Zustand den richtigen Kasten

    pytest tests/test_pfand_announced.py -v
"""
from __future__ import annotations

from datetime import date

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.main import app
from app.passwords import hash_password


SURNAMES = ("Paid", "Announced", "Exempt", "Nothing", "Returned")


@pytest.fixture(scope="module")
def ctx():
    SessionLocal, teardown = make_isolated_session_factory()
    db = SessionLocal()
    try:
        def mk(last, **kw):
            h = models.Helper(
                first_name="Test", last_name=last,
                email=f"{last.lower()}@example.org",
                date_of_birth=date(1995, 1, 1), status="registered",
                password_hash=hash_password("test1234"), **kw,
            )
            db.add(h)
            return h

        mk("Paid", pfand_paid=True)
        mk("Announced", pfand_announced=True, pfand_announced_due=date(2026, 8, 3))
        mk("Exempt", pfand_exempt=True)
        mk("Nothing")
        mk("Returned", pfand_paid=True, pfand_returned=True)
        db.commit()
        ids = {h.last_name: h.id for h in db.query(models.Helper).all()}
    finally:
        db.close()

    with TestClient(app) as c:
        yield c, SessionLocal, ids

    teardown()


def _admin(client):
    client.post("/admin/login", data={"username": "test-admin", "password": "test-pw-123"})


def _as_helper(client, surname):
    client.get("/logout")
    r = client.post("/login", data={"email": f"{surname.lower()}@example.org",
                                    "password": "test1234"})
    assert r.status_code == 200
    return r


def _names(html):
    return {n for n in SURNAMES if n in html}


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------
def test_paid_or_soon_matches_paid_and_announced(ctx):
    client, _, _ = ctx
    _admin(client)
    resp = client.get("/admin/helpers?pfand=paid_or_soon")
    assert resp.status_code == 200
    # Exempt und Nothing sind raus; Returned ist bezahlt und zaehlt mit.
    assert _names(resp.text) == {"Paid", "Announced", "Returned"}


def test_paid_or_soon_ignores_exempt(ctx):
    client, _, _ = ctx
    _admin(client)
    resp = client.get("/admin/helpers?pfand=paid_or_soon")
    assert "Exempt" not in resp.text


def test_plain_paid_filter_unchanged(ctx):
    client, _, _ = ctx
    _admin(client)
    resp = client.get("/admin/helpers?pfand=paid")
    # bezahlt, aber noch nicht zurueck -> nur Paid
    assert _names(resp.text) == {"Paid"}


def test_announced_shows_yellow_marker(ctx):
    client, _, _ = ctx
    _admin(client)
    resp = client.get("/admin/helpers?pfand=paid_or_soon")
    assert "Zahlung angekündigt" in resp.text
    assert "bg-amber-100" in resp.text


# ---------------------------------------------------------------------------
# Speicherlogik
# ---------------------------------------------------------------------------
def test_saving_paid_clears_announcement(ctx):
    client, SessionLocal, ids = ctx
    _admin(client)
    hid = ids["Announced"]
    client.post(f"/admin/helpers/{hid}/save", data={
        "section": "pfand", "pfand_paid": "on",
        "pfand_announced": "on", "pfand_announced_due": "2026-08-03",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        h = db.get(models.Helper, hid)
        assert h.pfand_paid is True
        assert h.pfand_announced is False, "Ankündigung muss bei Zahlung verfallen"
        assert h.pfand_announced_due is None
    finally:
        db.close()


def test_announcement_with_due_date_persists(ctx):
    client, SessionLocal, ids = ctx
    _admin(client)
    hid = ids["Nothing"]
    client.post(f"/admin/helpers/{hid}/save", data={
        "section": "pfand", "pfand_announced": "on",
        "pfand_announced_due": "2026-08-10",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        h = db.get(models.Helper, hid)
        assert h.pfand_announced is True
        assert h.pfand_announced_due == date(2026, 8, 10)
    finally:
        db.close()


def test_unchecking_announcement_clears_due_date(ctx):
    client, SessionLocal, ids = ctx
    _admin(client)
    hid = ids["Nothing"]
    client.post(f"/admin/helpers/{hid}/save", data={
        "section": "pfand", "pfand_announced_due": "2026-08-10",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        h = db.get(models.Helper, hid)
        assert h.pfand_announced is False
        assert h.pfand_announced_due is None
    finally:
        db.close()


def test_invalid_due_date_does_not_crash(ctx):
    client, SessionLocal, ids = ctx
    _admin(client)
    hid = ids["Exempt"]
    resp = client.post(f"/admin/helpers/{hid}/save", data={
        "section": "pfand", "pfand_exempt": "on",
        "pfand_announced": "on", "pfand_announced_due": "kein-datum",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    db = SessionLocal()
    try:
        h = db.get(models.Helper, hid)
        assert h.pfand_announced is True
        assert h.pfand_announced_due is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /me-Anzeige
# ---------------------------------------------------------------------------
def test_me_shows_paid_box(ctx):
    client, _, _ = ctx
    resp = _as_helper(client, "Paid")
    assert "Pfand erhalten" in resp.text
    assert "bg-emerald-50" in resp.text


def test_me_shows_returned_box(ctx):
    client, _, _ = ctx
    resp = _as_helper(client, "Returned")
    assert "Pfand zurücküberwiesen" in resp.text
    assert "rgb(255,179,137)" in resp.text
    # Der gruene Kasten darf daneben nicht auch erscheinen.
    assert "Pfand erhalten" not in resp.text


def test_me_shows_exempt_box(ctx):
    client, _, _ = ctx
    resp = _as_helper(client, "Exempt")
    assert "Für dich fällt kein Pfand an" in resp.text
    assert "bg-sky-50" in resp.text


def test_me_announcement_stays_internal(ctx):
    """Die interne Ankuendigung darf auf /me nicht auftauchen."""
    client, SessionLocal, ids = ctx
    db = SessionLocal()
    try:
        h = db.get(models.Helper, ids["Nothing"])
        h.pfand_announced = True
        h.pfand_announced_due = date(2026, 8, 10)
        db.commit()
    finally:
        db.close()

    resp = _as_helper(client, "Nothing")
    assert "Pfand noch offen" in resp.text
    assert "angekündigt" not in resp.text.lower()
