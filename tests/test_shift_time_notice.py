"""Tests fuer die Benachrichtigung bei geaenderter Schicht-Uhrzeit.

Abgedeckt:
  - Zeitaenderung -> Mail an alle eingetragenen Personen, mit alter UND neuer Zeit
  - Nur Label/Kapazitaet geaendert -> keine Mail
  - Leere Schicht verschoben -> keine Mail
  - Checkbox "notify" abgewaehlt -> keine Mail, Zeit trotzdem gespeichert
  - Ungueltige Uhrzeit -> keine Mail (nichts wurde gespeichert)

    pytest tests/test_shift_time_notice.py -v
"""
from __future__ import annotations

from datetime import date, time

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models
from app.main import app
from app.passwords import hash_password
from app.routers import admin_pages


@pytest.fixture
def outbox(monkeypatch):
    """Faengt den fertig gebauten Mailversand ab. Liste von prepared-Dicts.

    Gepatcht wird `deliver` in app.email_sender - also die Stelle NACH dem
    Textbau. So pruefen die Tests den echten Betreff und Text mit.
    """
    box = []

    def fake_deliver(prepared):
        box.append(prepared)

    from app import email_sender
    monkeypatch.setattr(email_sender, "deliver", fake_deliver)
    return box


@pytest.fixture
def ctx():
    SessionLocal, teardown = make_isolated_session_factory()
    db = SessionLocal()
    try:
        area = models.Area(name="Bar", sort_order=0)
        day = models.FestivalDay(date=date(2026, 8, 14), label="Freitag", sort_order=0)
        db.add_all([area, day])
        db.flush()

        besetzt = models.Shift(area_id=area.id, day_id=day.id, capacity=3,
                               start_time=time(18, 0), end_time=time(0, 0))
        leer = models.Shift(area_id=area.id, day_id=day.id, capacity=2,
                            start_time=time(12, 0), end_time=time(18, 0))
        db.add_all([besetzt, leer])
        db.flush()

        helpers = []
        for name in ("Ada", "Grace"):
            h = models.Helper(
                first_name=name, last_name="Test",
                email=f"{name.lower()}@example.org",
                date_of_birth=date(1995, 1, 1), status="registered",
                password_hash=hash_password("test1234"),
            )
            db.add(h)
            helpers.append(h)
        db.flush()
        for h in helpers:
            db.add(models.ShiftAssignment(shift_id=besetzt.id, helper_id=h.id))

        db.commit()
        ids = {"besetzt": besetzt.id, "leer": leer.id}
    finally:
        db.close()

    with TestClient(app) as c:
        c.post("/admin/login", data={"username": "test-admin", "password": "test-pw-123"})
        yield c, SessionLocal, ids

    teardown()


def _post(client, shift_id, *, start="18:00", end="00:00", label="",
          capacity="3", notify=True):
    data = {
        f"start_time_{shift_id}": start,
        f"end_time_{shift_id}": end,
        f"label_{shift_id}": label,
        f"capacity_{shift_id}": capacity,
    }
    if notify:
        data["notify"] = "on"
    return client.post("/admin/shifts/bulk-edit", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# Es wird benachrichtigt
# ---------------------------------------------------------------------------
def test_time_change_notifies_all_assigned(ctx, outbox):
    client, _, ids = ctx
    resp = _post(client, ids["besetzt"], start="20:00", end="02:00")
    assert resp.status_code == 303
    assert len(outbox) == 2
    assert {m["to_email"] for m in outbox} == {"ada@example.org", "grace@example.org"}


def test_mail_names_old_and_new_time(ctx, outbox):
    client, _, ids = ctx
    _post(client, ids["besetzt"], start="20:00", end="02:00")
    body = outbox[0]["body"]
    assert "18:00 – 00:00" in body, "alte Zeit muss drinstehen"
    assert "20:00 – 02:00" in body, "neue Zeit muss drinstehen"
    assert "verschoben" in outbox[0]["subject"]


def test_only_end_time_changed_still_notifies(ctx, outbox):
    client, _, ids = ctx
    _post(client, ids["besetzt"], start="18:00", end="23:00")
    assert len(outbox) == 2


def test_new_time_is_saved(ctx, outbox):
    client, SessionLocal, ids = ctx
    _post(client, ids["besetzt"], start="20:00", end="02:00")
    db = SessionLocal()
    try:
        s = db.get(models.Shift, ids["besetzt"])
        assert s.start_time == time(20, 0)
        assert s.end_time == time(2, 0)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Es wird NICHT benachrichtigt
# ---------------------------------------------------------------------------
def test_label_change_alone_sends_nothing(ctx, outbox):
    client, _, ids = ctx
    _post(client, ids["besetzt"], label="Spätschicht")
    assert outbox == []


def test_capacity_change_alone_sends_nothing(ctx, outbox):
    client, _, ids = ctx
    _post(client, ids["besetzt"], capacity="5")
    assert outbox == []


def test_unchanged_time_sends_nothing(ctx, outbox):
    client, _, ids = ctx
    _post(client, ids["besetzt"])
    assert outbox == []


def test_empty_shift_sends_nothing(ctx, outbox):
    client, _, ids = ctx
    resp = _post(client, ids["leer"], start="13:00", end="19:00", capacity="2")
    assert resp.status_code == 303
    assert outbox == []


def test_notify_unchecked_saves_but_stays_quiet(ctx, outbox):
    client, SessionLocal, ids = ctx
    _post(client, ids["besetzt"], start="20:00", end="02:00", notify=False)
    assert outbox == []

    db = SessionLocal()
    try:
        s = db.get(models.Shift, ids["besetzt"])
        assert s.start_time == time(20, 0), "Speichern darf nicht am Haken haengen"
    finally:
        db.close()


def test_invalid_time_sends_nothing(ctx, outbox):
    client, SessionLocal, ids = ctx
    resp = _post(client, ids["besetzt"], start="halb acht", end="02:00")
    assert resp.status_code == 303
    assert outbox == []

    db = SessionLocal()
    try:
        s = db.get(models.Shift, ids["besetzt"])
        assert s.start_time == time(18, 0)
        assert s.end_time == time(0, 0), "abgebrochener Speichervorgang darf nichts halb setzen"
    finally:
        db.close()
