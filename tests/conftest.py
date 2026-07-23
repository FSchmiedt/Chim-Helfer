"""Gemeinsames Test-Setup fuer alle Dateien unter tests/.

Setzt nur die Env-Variablen, die app.config.settings (ein Singleton, das
beim ERSTEN Import ausgewertet wird) braucht, damit `from app.main import
app` in den einzelnen Testdateien nicht mit fehlenden/falschen Werten
stolpert. `setdefault`, falls schon was gesetzt ist.

Die eigentliche Test-Datenbank wird NICHT hier zentral verwaltet: jede
Testdatei bekommt ihre eigene, isolierte SQLite-Datei ueber ein
FastAPI-`dependency_overrides[get_db]` (siehe die `client`-Fixture in den
einzelnen Testdateien). Grund: Die globale Engine aus app.database ist ein
Singleton - wuerden mehrere Testdateien sich die eine Datei teilen und am
Ende jeweils "ihre" Datei loeschen, reisst die zuerst fertige Datei den noch
laufenden Tests der anderen Datei den Boden unter den Fuessen weg (genau
das ist uns beim Hinzufuegen von test_discount_selfservice.py neben
test_filters_smoke.py passiert). Mit dependency_overrides ist jede
Testdatei komplett fuer sich isoliert, unabhaengig von Reihenfolge.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-pw-123")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-prod")
os.environ.setdefault("MIN_SHIFTS", "2")
os.environ.setdefault("MIN_DAYS", "2")
