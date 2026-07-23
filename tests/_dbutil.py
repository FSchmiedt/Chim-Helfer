"""Hilfsfunktion fuer isolierte Test-Datenbanken.

Jede Testdatei bekommt ihre eigene SQLite-Datei, komplett unabhaengig von
der globalen Engine in app.database (die ist ein Prozess-Singleton und
sollte in Tests nicht direkt angefasst werden - siehe conftest.py fuer den
Hintergrund, warum das schiefging).
"""
from __future__ import annotations

import os
import tempfile
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app


def make_isolated_session_factory():
    """Frische SQLite-Testdatenbank, per dependency_overrides in die App
    eingehaengt. Rueckgabe: (SessionFactory, teardown_fn).

    teardown_fn() muss am Ende der Fixture aufgerufen werden (raeumt
    dependency_overrides, Engine und Tempfile wieder auf).
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    test_engine = create_engine(
        f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}
    )
    TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=test_engine)

    def _override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db

    def teardown() -> None:
        app.dependency_overrides.pop(get_db, None)
        test_engine.dispose()
        os.unlink(tmp.name)

    return TestSessionLocal, teardown
