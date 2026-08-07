"""Tests für den Direkt-Flow (Schicht-zuerst-Anmeldung auf "/").

Deckt ab:
- "/" zeigt bei offenem Direkt-Schalter den neuen Flow, sonst das alte Formular
- /vorschau ist ohne gültiges Token nicht erreichbar (404), mit Token schon
- POST /mitmachen legt Person + Zuweisungen an und loggt automatisch ein
- 75€-Ein-Schicht-Ticket (only_one) setzt wants_only_one_shift + discount_offered
- leeres Passwort ist erlaubt (kein Login-Passwort gesetzt)
- Kautions-Antwort landet als Notiz
- Schutzregeln: bestehende Email, max. Schichtzahl, Volljährigkeit, volle Schicht
- geschlossener Schalter blockt POST (403), gültiges Preview-Token umgeht das

Der Direkt-Schalter wird pro Test über das settings-Singleton gesetzt
(monkeypatch), weil settings beim Import einmalig ausgewertet wird.
"""
from __future__ import annotations

from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.config import settings
from app.main import app


PREVIEW_TOKEN = "test-preview-token-xyz"


@pytest.fixture(scope="module")
def session_local():
    SessionLocal, teardown = make_isolated_session_factory()
    # Seed: 2 Tage, Bar + Einlass, einige Schichten
    db = SessionLocal()
    try:
        d1 = models.FestivalDay(date=date(2026, 8, 14), label="Freitag", sort_order=0)
        d2 = models.FestivalDay(date=date(2026, 8, 15), label="Samstag", sort_order=1)
        bar = models.Area(name="Bar", sort_order=0)
        einlass = models.Area(name="Einlass", sort_order=1)
        db.add_all([d1, d2, bar, einlass])
        db.flush()
        db.add_all([
            models.Shift(area_id=bar.id, day_id=d1.id, label="Hauptbar",
                         start_time=time(20, 0), end_time=time(23, 0), capacity=2),
            # Bewusst grosszuegig: mehrere Tests buchen hier nacheinander, und
            # die Fixture ist modulweit (Buchungen bleiben also stehen). Zu
            # knapp bemessen heisst: der letzte Test scheitert an "voll" statt
            # an dem, was er eigentlich prueft.
            models.Shift(area_id=einlass.id, day_id=d1.id,
                         start_time=time(18, 0), end_time=time(21, 0), capacity=10),
            models.Shift(area_id=einlass.id, day_id=d2.id,
                         start_time=time(18, 0), end_time=time(21, 0), capacity=1),
        ])
        db.commit()
    finally:
        db.close()
    yield SessionLocal
    teardown()


@pytest.fixture(scope="module")
def client(session_local):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def direct_open(monkeypatch):
    """Direkt-Flow offen + Preview-Token gesetzt."""
    monkeypatch.setattr(settings, "DIRECT_SIGNUP_OPEN", True)
    monkeypatch.setattr(settings, "DIRECT_SIGNUP_OPEN_AT", "")
    monkeypatch.setattr(settings, "DIRECT_SIGNUP_PREVIEW_TOKEN", PREVIEW_TOKEN)


@pytest.fixture
def direct_closed(monkeypatch):
    """Direkt-Flow geschlossen, Preview-Token trotzdem gesetzt."""
    monkeypatch.setattr(settings, "DIRECT_SIGNUP_OPEN", False)
    monkeypatch.setattr(settings, "DIRECT_SIGNUP_OPEN_AT", "")
    monkeypatch.setattr(settings, "DIRECT_SIGNUP_PREVIEW_TOKEN", PREVIEW_TOKEN)
    monkeypatch.setattr(settings, "REGISTRATION_OPEN", True)


def _shift_id(session_local, area_name, day_label):
    db = session_local()
    try:
        return (
            db.query(models.Shift)
            .join(models.Area).join(models.FestivalDay)
            .filter(models.Area.name == area_name, models.FestivalDay.label == day_label)
            .first().id
        )
    finally:
        db.close()


def _get_helper(session_local, email):
    """Helper laden UND die in Tests geprüften Relationen noch in der offenen
    Session materialisieren (sonst DetachedInstanceError beim Lazy-Load)."""
    db = session_local()
    try:
        h = db.query(models.Helper).filter(models.Helper.email == email).one_or_none()
        if h is not None:
            _ = len(h.shift_assignments)
            _ = len(h.availabilities)
        return h
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Anzeige
# ---------------------------------------------------------------------------
def test_root_shows_direct_flow_when_open(client, direct_open):
    r = client.get("/")
    assert r.status_code == 200
    assert "Such dir deine Schicht" in r.text
    # Bar-Hinweis wird eingeblendet
    assert "Bar-Erfahrung" in r.text


def test_root_shows_classic_form_when_closed(client, direct_closed):
    r = client.get("/")
    assert r.status_code == 200
    assert "Such dir deine Schicht" not in r.text


def test_preview_requires_token(client, direct_open):
    assert client.get("/vorschau").status_code == 404
    assert client.get("/vorschau?key=falsch").status_code == 404
    r = client.get(f"/vorschau?key={PREVIEW_TOKEN}")
    assert r.status_code == 200
    assert "Vorschau (interner Testzugang)" in r.text
    assert 'name="preview_token"' in r.text


# ---------------------------------------------------------------------------
# Erfolgreiche Eintragung
# ---------------------------------------------------------------------------
def test_two_shifts_signup_creates_helper_and_logs_in(client, session_local, direct_open):
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    ein_sa = _shift_id(session_local, "Einlass", "Samstag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr, ein_sa],
        "first_name": "Anna", "last_name": "Test", "email": "anna@example.org",
        "phone": "0170",
        "password": "ingwer-482", "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/me")
    assert "chimaera_helper_session" in r.headers.get("set-cookie", "")

    h = _get_helper(session_local, "anna@example.org")
    assert h is not None
    assert len(h.shift_assignments) == 2
    assert h.password_hash  # gesetzt
    assert h.wants_only_one_shift is False
    assert h.notes and "kann ich zahlen" in h.notes
    assert len(h.availabilities) == 2


def test_one_shift_ticket_sets_discount_and_allows_empty_password(client, session_local, direct_open):
    bar_fr = _shift_id(session_local, "Bar", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [bar_fr], "only_one_shift": "on",
        "first_name": "Bea", "last_name": "Bar", "email": "bea@example.org",
        "password": "",
        "deposit_ok": "partial",
    }, follow_redirects=False)
    assert r.status_code == 303
    h = _get_helper(session_local, "bea@example.org")
    assert h.wants_only_one_shift is True
    assert h.discount_offered is True
    assert h.password_hash is None  # leer erlaubt
    assert "80 €" in h.notes


# ---------------------------------------------------------------------------
# Schutzregeln
# ---------------------------------------------------------------------------
def test_existing_email_is_rejected_with_login_hint(client, session_local, direct_open):
    bar_fr = _shift_id(session_local, "Bar", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [bar_fr],
        "first_name": "Anna", "last_name": "Nochmal", "email": "anna@example.org",
        "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 400
    assert "schon ein Konto" in r.text


def test_more_than_two_shifts_rejected(client, session_local, direct_open):
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    bar_fr = _shift_id(session_local, "Bar", "Freitag")
    ein_sa = _shift_id(session_local, "Einlass", "Samstag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr, bar_fr, ein_sa],
        "first_name": "Cim", "last_name": "Drei", "email": "cim@example.org",
        "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 400
    assert "höchstens zwei" in r.text


def test_no_age_fields_asked(client, session_local, direct_open):
    """Geburtsdatum und Volljaehrigkeits-Haken sind raus - Ausweis am Eingang."""
    r = client.get("/")
    assert 'name="date_of_birth"' not in r.text
    assert 'name="is_adult_confirmed"' not in r.text
    assert "18 Jahre" not in r.text


def test_signup_without_birthdate_uses_placeholder(client, session_local, direct_open):
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr],
        "first_name": "Ohne", "last_name": "Datum", "email": "ohnedatum@example.org",
        "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = session_local()
    try:
        h = db.query(models.Helper).filter_by(email="ohnedatum@example.org").one()
        # Platzhalter statt NULL: die Spalte ist NOT NULL, das Alter wird am
        # Eingang geprueft.
        assert h.date_of_birth == date(1990, 1, 1)
    finally:
        db.close()


def test_missing_deposit_answer_rejected(client, session_local, direct_open):
    bar_fr = _shift_id(session_local, "Bar", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [bar_fr],
        "first_name": "Kai", "last_name": "Ohne", "email": "kai@example.org",
       
    }, follow_redirects=False)
    assert r.status_code == 400
    assert "Kaution" in r.text


# ---------------------------------------------------------------------------
# Schalter zu
# ---------------------------------------------------------------------------
def test_closed_switch_blocks_post(client, session_local, direct_closed):
    bar_fr = _shift_id(session_local, "Bar", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [bar_fr],
        "first_name": "X", "last_name": "Y", "email": "closed@example.org",
        "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 403


def test_valid_preview_token_bypasses_closed_switch(client, session_local, direct_closed):
    ein_sa = _shift_id(session_local, "Einlass", "Samstag")
    # Samstag-Einlass hat capacity 1; falls von einem früheren Test belegt,
    # nehmen wir Bar Freitag (capacity 2) - hier aber eigener Bereich frei halten:
    bar_fr = _shift_id(session_local, "Bar", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [bar_fr], "preview_token": PREVIEW_TOKEN,
        "first_name": "Vor", "last_name": "Schau", "email": "preview@example.org",
        "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_helper(session_local, "preview@example.org") is not None


# ---------------------------------------------------------------------------
# Reservierungen ("Holds") — Kinoticket-Prinzip
# ---------------------------------------------------------------------------
def _hold_token(client):
    """Frischen hold_token aus einem GET / ziehen."""
    import re
    html = client.get("/").text
    m = re.search(r'name="hold_token" value="([^"]+)"', html)
    assert m, "hold_token nicht im Formular gefunden"
    return m.group(1)


def _make_shift(session_local, area_name, day_label, cap, start_h):
    """Legt eine frische Schicht an und gibt ihre id zurück (Test-Isolation)."""
    from datetime import time as _t
    db = session_local()
    try:
        area = db.query(models.Area).filter_by(name=area_name).one()
        day = db.query(models.FestivalDay).filter_by(label=day_label).one()
        s = models.Shift(area_id=area.id, day_id=day.id,
                         start_time=_t(start_h, 0), end_time=_t(start_h + 2, 0),
                         capacity=cap)
        db.add(s)
        db.commit()
        return s.id
    finally:
        db.close()


def _clear_holds(session_local):
    db = session_local()
    try:
        db.query(models.ShiftHold).delete()
        db.commit()
    finally:
        db.close()


def test_hold_blocks_second_person_on_full_shift(client, session_local, direct_open):
    """capacity-1-Schicht: wer zuerst reserviert, hält sie; der zweite fliegt raus."""
    sid = _make_shift(session_local, "Bar", "Freitag", cap=1, start_h=6)
    tok_a = _hold_token(client)
    tok_b = _hold_token(client)
    assert tok_a != tok_b

    r = client.post("/mitmachen/hold", data={"shift_ids": [sid], "hold_token": tok_a})
    assert r.json()["ok"] is True

    r = client.post("/mitmachen/hold", data={"shift_ids": [sid], "hold_token": tok_b})
    body = r.json()
    assert body["ok"] is False
    assert len(body["unavailable"]) == 1
    assert body["unavailable"][0]["id"] == sid
    _clear_holds(session_local)


def test_held_shift_disappears_from_listing(client, session_local, direct_open):
    sid = _make_shift(session_local, "Bar", "Freitag", cap=1, start_h=8)
    tok_a = _hold_token(client)
    client.post("/mitmachen/hold", data={"shift_ids": [sid], "hold_token": tok_a})

    # Ein anderer Betrachter (frischer Token) sieht die Schicht nicht mehr.
    html = client.get("/").text
    assert f'value="{sid}"' not in html
    _clear_holds(session_local)


def test_own_hold_is_released_after_booking(client, session_local, direct_open):
    sid = _make_shift(session_local, "Bar", "Freitag", cap=2, start_h=10)
    tok = _hold_token(client)
    client.post("/mitmachen/hold", data={"shift_ids": [sid], "hold_token": tok})

    r = client.post("/mitmachen", data={
        "shift_ids": [sid], "hold_token": tok,
        "first_name": "Held", "last_name": "Frei", "email": "held@example.org",
        "deposit_ok": "yes",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = session_local()
    try:
        remaining = db.query(models.ShiftHold).filter(models.ShiftHold.token == tok).count()
        assert remaining == 0
    finally:
        db.close()


def test_hold_requires_token_and_shifts(client, session_local, direct_open):
    r = client.post("/mitmachen/hold", data={"hold_token": ""})
    assert r.status_code == 400
    tok = _hold_token(client)
    r = client.post("/mitmachen/hold", data={"hold_token": tok})  # keine shift_ids
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Kautions-Optionen (voll / Teilbetrag / gar nicht mit Begruendung)
# ---------------------------------------------------------------------------
def test_partial_deposit_option_is_offered(client, session_local, direct_open):
    r = client.get("/")
    assert 'value="partial"' in r.text
    assert "80" in r.text


def test_partial_deposit_is_saved(client, session_local, direct_open):
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr],
        "first_name": "Teil", "last_name": "Zahler", "email": "teil@example.org",
        "deposit_ok": "partial",
    }, follow_redirects=False)
    assert r.status_code == 303
    h = _get_helper(session_local, "teil@example.org")
    assert "80 €" in h.notes


def test_no_deposit_requires_reason(client, session_local, direct_open):
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr],
        "first_name": "Ohne", "last_name": "Grund", "email": "ohnegrund@example.org",
        "deposit_ok": "no",
    }, follow_redirects=False)
    assert r.status_code == 400
    assert "verlassen" in r.text


def test_no_deposit_with_reason_is_saved(client, session_local, direct_open):
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr],
        "first_name": "Mit", "last_name": "Grund", "email": "mitgrund@example.org",
        "deposit_ok": "no",
        "deposit_alternative": "ich baue seit Jahren mit auf",
    }, follow_redirects=False)
    assert r.status_code == 303
    h = _get_helper(session_local, "mitgrund@example.org")
    assert "Begründung" in h.notes
    assert "seit Jahren" in h.notes


def test_one_shift_ticket_may_not_skip_deposit(client, session_local, direct_open):
    """Beim 75-Euro-Ticket ist "geht gar nicht" im Formular ausgeblendet -
    der Server muss es zusaetzlich abweisen, falls es doch mitgeschickt wird."""
    ein_fr = _shift_id(session_local, "Einlass", "Freitag")
    r = client.post("/mitmachen", data={
        "shift_ids": [ein_fr], "only_one_shift": "on",
        "first_name": "Schlau", "last_name": "Fuchs", "email": "fuchs@example.org",
        "deposit_ok": "no", "deposit_alternative": "trust me",
    }, follow_redirects=False)
    assert r.status_code == 400
    assert "Ein-Schicht-Ticket" in r.text


# ---------------------------------------------------------------------------
# Startseite: Login-Box und Ueberschrift
# ---------------------------------------------------------------------------
def test_index_has_login_box_and_heading(client, session_local, direct_open):
    r = client.get("/")
    assert "Hier einloggen" in r.text
    assert "Noch offene Schichten" in r.text


def test_phone_hint_wording(client, session_local, direct_open):
    r = client.get("/")
    assert "Hilfreich, wenn du über Handy" in r.text
    assert "Optional. Falls du dort" not in r.text
