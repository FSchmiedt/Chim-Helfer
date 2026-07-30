"""Tests fuer den Durchlauf ueberfaelliger Pfand-Zusagen (app/pfand_promises.py).

Der Durchlauf ersetzt einen Cron-Job und haengt an Admin-Requests. Kritisch
sind drei Dinge, und genau die stehen hier:

  1. Die Kante bei Frist + GRACE_DAYS - einen Tag zu frueh austragen waere
     ein stiller Datenverlust.
  2. Idempotenz - derselbe Aufruf zweimal darf keine zweite Mail ausloesen.
  3. Der Durchlauf darf nie eine Admin-Seite kaputt machen.

Mails werden nicht wirklich verschickt: send_mail wird gemonkeypatcht.

    pytest tests/test_pfand_promise_sweep.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from fastapi.testclient import TestClient

from _dbutil import make_isolated_session_factory
from app import models, pfand_promises
from app.main import app


TODAY = date(2026, 8, 5)  # Mittwoch


@pytest.fixture
def sent(monkeypatch):
    """Faengt Mailversand ab. Liste von (empfaenger, betreff, body)."""
    box = []

    def fake_send_mail(to_addresses, subject, body, bcc=True):
        box.append((to_addresses, subject, body))
        return len(to_addresses)

    monkeypatch.setattr(pfand_promises, "send_mail", fake_send_mail)
    monkeypatch.setattr(pfand_promises, "today_local", lambda: TODAY)
    return box


@pytest.fixture
def db_factory():
    SessionLocal, teardown = make_isolated_session_factory()
    yield SessionLocal
    teardown()


def _mk(db, last, due, **kw):
    h = models.Helper(
        first_name="Test", last_name=last, email=f"{last.lower()}@example.org",
        date_of_birth=date(1995, 1, 1), status="registered",
        pfand_announced=True, pfand_announced_due=due, **kw,
    )
    db.add(h)
    db.flush()
    return h


# ---------------------------------------------------------------------------
# Kante bei Frist + 3
# ---------------------------------------------------------------------------
def test_not_cleared_one_day_early(db_factory, sent):
    """Frist Montag -> am Mittwoch (Frist+2) noch NICHT austragen."""
    db = db_factory()
    try:
        h = _mk(db, "Zwei", TODAY - timedelta(days=2))
        db.commit()
        result = pfand_promises.run_sweep(db)
        db.refresh(h)
        assert h.pfand_announced is True, "zu frueh ausgetragen"
        assert result["cleared"] == 0
    finally:
        db.close()


def test_cleared_exactly_on_grace_day(db_factory, sent):
    """Frist Montag -> am Donnerstag (Frist+3) austragen."""
    db = db_factory()
    try:
        h = _mk(db, "Drei", TODAY - timedelta(days=3))
        db.commit()
        result = pfand_promises.run_sweep(db)
        db.refresh(h)
        assert h.pfand_announced is False
        assert h.pfand_announced_due is None
        assert h.pfand_announced_notified_at is None
        assert result["cleared"] == 1
    finally:
        db.close()


def test_due_today_does_nothing(db_factory, sent):
    """Am Tag der Frist selbst passiert noch gar nichts."""
    db = db_factory()
    try:
        h = _mk(db, "Heute", TODAY)
        db.commit()
        result = pfand_promises.run_sweep(db)
        db.refresh(h)
        assert h.pfand_announced is True
        assert h.pfand_announced_notified_at is None
        assert result == {"notified": 0, "cleared": 0}
        assert sent == []
    finally:
        db.close()


def test_notified_day_after_deadline(db_factory, sent):
    db = db_factory()
    try:
        h = _mk(db, "Gestern", TODAY - timedelta(days=1))
        db.commit()
        result = pfand_promises.run_sweep(db)
        db.refresh(h)
        assert result["notified"] == 1
        assert h.pfand_announced_notified_at is not None
        assert h.pfand_announced is True, "melden heisst noch nicht austragen"
        assert len(sent) == 1
        assert "Gestern" in sent[0][2]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idempotenz
# ---------------------------------------------------------------------------
def test_second_run_sends_nothing(db_factory, sent):
    db = db_factory()
    try:
        _mk(db, "Einmal", TODAY - timedelta(days=1))
        db.commit()
        pfand_promises.run_sweep(db)
        pfand_promises.run_sweep(db)
        pfand_promises.run_sweep(db)
        assert len(sent) == 1, "Zusage wurde mehrfach gemeldet"
    finally:
        db.close()


def test_paid_helper_is_never_notified(db_factory, sent):
    """Wer bezahlt hat, taucht nicht in der Mahnliste auf."""
    db = db_factory()
    try:
        _mk(db, "Bezahlt", TODAY - timedelta(days=1), pfand_paid=True)
        db.commit()
        result = pfand_promises.run_sweep(db)
        assert result["notified"] == 0
        assert sent == []
    finally:
        db.close()


def test_no_due_date_is_ignored(db_factory, sent):
    """Zusage ohne Frist laeuft nie ab - sonst wuerde sie sofort verschwinden."""
    db = db_factory()
    try:
        h = _mk(db, "Fristlos", None)
        db.commit()
        result = pfand_promises.run_sweep(db)
        db.refresh(h)
        assert h.pfand_announced is True
        assert result == {"notified": 0, "cleared": 0}
    finally:
        db.close()


def test_digest_lists_both_sections(db_factory, sent):
    db = db_factory()
    try:
        _mk(db, "Neu", TODAY - timedelta(days=1))
        _mk(db, "Alt", TODAY - timedelta(days=4))
        db.commit()
        result = pfand_promises.run_sweep(db)
        assert result == {"notified": 2, "cleared": 1}
        body = sent[0][2]
        assert "Neu" in body and "Alt" in body
        assert "Frist überschritten" in body
        assert "Automatisch ausgetragen" in body
        assert len(sent) == 1, "es soll eine Sammelmail sein, nicht eine pro Person"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Robustheit
# ---------------------------------------------------------------------------
def test_run_sweep_safe_swallows_errors(db_factory, monkeypatch):
    db = db_factory()
    try:
        def boom(*a, **kw):
            raise RuntimeError("DB weg")
        monkeypatch.setattr(pfand_promises, "_overdue_query", boom)
        assert pfand_promises.run_sweep_safe(db) == {"notified": 0, "cleared": 0}
    finally:
        db.close()


def test_admin_pages_still_work_when_sweep_fails(db_factory, monkeypatch):
    """Ein kaputter Durchlauf darf die Helferuebersicht nicht mitreissen."""
    def boom(*a, **kw):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(pfand_promises, "run_sweep", boom)

    with TestClient(app) as client:
        client.post("/admin/login", data={"username": "test-admin",
                                          "password": "test-pw-123"})
        resp = client.get("/admin/helpers")
        assert resp.status_code == 200
