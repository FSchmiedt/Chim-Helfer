"""Regression-Test: Selbst-Anmeldung zum 75€-Ticket via /me.

Hintergrund: Admin-seitig kann "wants_only_one_shift" aus rein
organisatorischen Gruenden gesetzt werden (z.B. Aufbau-Helfer:innen, die nur
noch 1 Zusatzschicht brauchen), OHNE dass ein 75€-Ticket im Spiel ist - dafuer
gibt es das separate "discount_offered"-Feld. Auf der Helfer:innen-eigenen
/me-Seite ist "wants_only_one_shift" dagegen IMMER das 75€-Ticket (die Person
entscheidet sich selbst dafuer, jederzeit moeglich). Deshalb muss der
Self-Service-Endpoint discount_offered synchron mitziehen - sonst zeigt das
Tool fuer diese Faelle faelschlich keinen Hinweis mehr an.
"""
from __future__ import annotations

from datetime import date

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.main import app
from app.passwords import hash_password


@pytest.fixture(scope="module")
def session_local():
    SessionLocal, teardown = make_isolated_session_factory()
    yield SessionLocal
    teardown()


@pytest.fixture(scope="module")
def client(session_local):
    db = session_local()
    try:
        helper = models.Helper(
            first_name="Selbst", last_name="Anmelder", email="selbst@example.org",
            date_of_birth=date(1990, 1, 1),
            password_hash=hash_password("test1234"),
        )
        db.add(helper)
        db.commit()
    finally:
        db.close()

    with TestClient(app) as c:
        yield c


def _get_helper(session_local, email="selbst@example.org"):
    db = session_local()
    try:
        return db.query(models.Helper).filter(models.Helper.email == email).one()
    finally:
        db.close()


def test_self_service_checkin_sets_discount_offered_without_mail(client, session_local):
    login = client.post("/login", data={"email": "selbst@example.org", "password": "test1234"})
    assert login.status_code == 200  # landet nach Redirects auf /me

    resp = client.post("/me/shift-preference", data={"wants_only_one_shift": "on"},
                        follow_redirects=False)
    assert resp.status_code == 303

    h = _get_helper(session_local)
    assert h.wants_only_one_shift is True
    assert h.discount_offered is True
    assert h.discount_offered_at is not None


def test_self_service_uncheck_clears_discount_offered(client, session_local):
    client.post("/login", data={"email": "selbst@example.org", "password": "test1234"})
    resp = client.post("/me/shift-preference", data={}, follow_redirects=False)
    assert resp.status_code == 303

    h = _get_helper(session_local)
    assert h.wants_only_one_shift is False
    assert h.discount_offered is False
    assert h.discount_offered_at is None


def test_admin_set_one_shift_without_discount_shows_no_badge(client, session_local):
    """Admin-seitiges 'wants_only_one_shift' fuer organisatorische Gruende
    (z.B. Aufbau-Helfer:in) darf discount_offered NICHT beruehren."""
    db = session_local()
    try:
        aufbau = models.Helper(
            first_name="Aufbau", last_name="Helferin", email="aufbau@example.org",
            date_of_birth=date(1991, 2, 2),
            wants_only_one_shift=True, discount_offered=False,
        )
        db.add(aufbau)
        db.commit()
    finally:
        db.close()

    client.post("/admin/login", data={"username": "test-admin", "password": "test-pw-123"})
    html = client.get("/admin/helpers").text
    idx = html.find("Aufbau Helferin")
    assert "⚠" not in html[idx:idx + 250]


def test_admin_discount_checkbox_forces_one_shift(client, session_local):
    """Admin hakt NUR '75€-Angebot machen' an (vergisst/laesst 'nur 1
    Schicht' unangetastet) - der Server muss wants_only_one_shift trotzdem
    auf True setzen, sonst Soll=2 trotz Ticket."""
    db = session_local()
    try:
        h = models.Helper(
            first_name="Nur", last_name="Ticket", email="nurticket@example.org",
            date_of_birth=date(1992, 3, 3),
            wants_only_one_shift=False, discount_offered=False,
        )
        db.add(h)
        db.commit()
        hid = h.id
    finally:
        db.close()

    client.post("/admin/login", data={"username": "test-admin", "password": "test-pw-123"})
    resp = client.post(f"/admin/helpers/{hid}/save", data={
        "section": "discount",
        "discount_offered": "on",
        # bewusst OHNE wants_only_one_shift im Formular
    }, follow_redirects=False)
    assert resp.status_code == 303

    db = session_local()
    try:
        h = db.get(models.Helper, hid)
        assert h.discount_offered is True
        assert h.wants_only_one_shift is True  # serverseitig erzwungen
    finally:
        db.close()


def test_admin_one_shift_alone_does_not_force_discount(client, session_local):
    """Umgekehrt bleibt erlaubt: 'nur 1 Schicht' ohne Ticket (Aufbau-Fall) -
    die Erzwingung ist bewusst einseitig."""
    db = session_local()
    try:
        h = models.Helper(
            first_name="Nur", last_name="Organisatorisch", email="organisatorisch@example.org",
            date_of_birth=date(1993, 4, 4),
        )
        db.add(h)
        db.commit()
        hid = h.id
    finally:
        db.close()

    client.post("/admin/login", data={"username": "test-admin", "password": "test-pw-123"})
    resp = client.post(f"/admin/helpers/{hid}/save", data={
        "section": "discount",
        "wants_only_one_shift": "on",
        # bewusst OHNE discount_offered im Formular
    }, follow_redirects=False)
    assert resp.status_code == 303

    db = session_local()
    try:
        h = db.get(models.Helper, hid)
        assert h.wants_only_one_shift is True
        assert h.discount_offered is False  # bleibt unangetastet
    finally:
        db.close()
