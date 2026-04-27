"""DB-Initialisierung + Seed-Daten + idempotente Spalten-Migrationen.

Usage:
    python init_db.py              # Tabellen anlegen + Migrationen + Default-Bereiche/Rollen
    python init_db.py --with-days  # zusätzlich Beispiel-Festivaltage
    python init_db.py --reset      # Tabellen löschen und neu anlegen (⚠️ löscht alles!)

Die Migrationen sind absichtlich simpel (ALTER TABLE ADD COLUMN auf bekannte neue
Spalten, mit try/except bei Already-Exists). Für das Feature-Set eines einmal-im-
Jahr-Festivals ist das bewusst pragmatischer als Alembic einzurichten.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from sqlalchemy import inspect, text

from app.database import Base, SessionLocal, engine
from app import models


DEFAULT_AREAS = [
    ("Verkehr", "", []),
    ("Einlass", "", ["Schichtleitung Einlass", "Ticketscan", "Bändchen"]),
    ("Cleaning", "", []),
    ("Bar", "", ["Barleitung", "Springer", "Tresenkraft", "Runner"]),
    ("Crew Catering", "", ["Küchenleitung", "Küchenhilfe"]),
    ("Driver", "", []),
    ("Abbau", "", ["Crewchef Abbau", "Abbauhelfer:in"]),
    ("Awareness", "", []),
]


# Spalten, die in früheren Versionen noch nicht existierten.
# Dialekt-agnostisch formuliert (funktioniert in SQLite + Postgres).
NEW_COLUMNS_BY_TABLE = {
    "helpers": [
        ("password_hash", "VARCHAR(255)"),
        ("password_reset_token", "VARCHAR(100)"),
        ("password_reset_expires", "TIMESTAMP"),
        ("email_verified_at", "TIMESTAMP"),
        ("email_verification_token", "VARCHAR(100)"),
        # Booleans ohne DEFAULT-Klausel, weil SQLite und Postgres sich hier
        # unterschiedlich verhalten. Wir backfillen weiter unten.
        ("pfand_paid", "BOOLEAN"),
        ("pfand_paid_at", "TIMESTAMP"),
        ("pfand_returned", "BOOLEAN"),
        ("pfand_returned_at", "TIMESTAMP"),
    ],
}

# Zusätzliche Backfills nach ADD COLUMN (für NOT NULL-artige Defaults).
BACKFILLS = [
    ("helpers", "UPDATE helpers SET pfand_paid = 0 WHERE pfand_paid IS NULL"),
    ("helpers", "UPDATE helpers SET pfand_returned = 0 WHERE pfand_returned IS NULL"),
]


def migrate_columns():
    """Fügt fehlende Spalten auf bestehenden Tabellen hinzu + Backfills."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols in NEW_COLUMNS_BY_TABLE.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_type in cols:
                if col_name in existing:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                    print(f"  + Spalte {table}.{col_name} ergänzt")
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! Spalte {table}.{col_name} konnte nicht ergänzt werden: {exc}")
        # Backfill-Queries (idempotent): setzen NULL auf definierten Wert.
        for table, sql in BACKFILLS:
            if table in inspector.get_table_names():
                try:
                    conn.execute(text(sql))
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! Backfill auf {table} fehlgeschlagen: {exc}")


def seed_areas_and_roles(db):
    """Legt Default-Bereiche mit Rollen an, wenn noch keine existieren."""
    existing = {a.name for a in db.query(models.Area).all()}
    for idx, (name, desc, role_names) in enumerate(DEFAULT_AREAS):
        if name in existing:
            continue
        area = models.Area(name=name, description=desc, sort_order=idx)
        db.add(area)
        db.flush()
        for r_idx, r_name in enumerate(role_names):
            db.add(models.Role(area_id=area.id, name=r_name, sort_order=r_idx))
    db.commit()


def seed_example_days(db):
    """Beispiel-Festivaltage Fr/Sa/So in ca. 3 Monaten."""
    if db.query(models.FestivalDay).count() > 0:
        return
    start = date.today() + timedelta(days=90)
    # Nächster Freitag
    while start.weekday() != 4:  # 4 = Freitag
        start += timedelta(days=1)
    labels = ["Freitag", "Samstag", "Sonntag"]
    for i, lbl in enumerate(labels):
        db.add(models.FestivalDay(date=start + timedelta(days=i), label=lbl, sort_order=i))
    db.commit()


def reseed_areas(db):
    """⚠️ Löscht alle Bereiche + Rollen und seedet die DEFAULT_AREAS neu.

    Cascade entfernt damit auch alle bestehenden HelperAreaPreference-Einträge,
    HelperRoleTrust-Einträge und Shifts. Das ist heftig — nur verwenden,
    bevor sich Helfer:innen angemeldet haben oder du die Konsequenzen kennst.
    """
    db.query(models.Area).delete()
    db.commit()
    seed_areas_and_roles(db)


def reseed_days(db):
    """⚠️ Löscht alle Festivaltage + zugehörige Verfügbarkeiten + Schichten und
    seedet die Beispieltage Fr/Sa/So neu. Wie bei reseed_areas: heftiger Eingriff."""
    db.query(models.FestivalDay).delete()
    db.commit()
    seed_example_days(db)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Alle Tabellen löschen und neu anlegen")
    parser.add_argument("--with-days", action="store_true", help="Beispiel-Festivaltage Fr/Sa/So anlegen")
    parser.add_argument("--reseed-areas", action="store_true",
                        help="⚠️ Bereiche+Rollen löschen und die aktuellen Defaults neu anlegen")
    parser.add_argument("--reseed-days", action="store_true",
                        help="⚠️ Festivaltage löschen und Fr/Sa/So neu anlegen")
    args = parser.parse_args()

    if args.reset:
        print("⚠️  Alle Tabellen werden gelöscht!")
        Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)
    print("✓ Tabellen angelegt / verifiziert")

    migrate_columns()
    print("✓ Spalten-Migrationen verifiziert")

    db = SessionLocal()
    try:
        if args.reseed_areas:
            reseed_areas(db)
            print("✓ Bereiche+Rollen RE-SEEDED")
        else:
            seed_areas_and_roles(db)
            print("✓ Default-Bereiche + Rollen geseedet (übersprungen falls vorhanden)")

        if args.reseed_days:
            reseed_days(db)
            print("✓ Festivaltage RE-SEEDED auf Fr/Sa/So")
        elif args.with_days:
            seed_example_days(db)
            print("✓ Beispiel-Festivaltage Fr/Sa/So angelegt")

        count = db.query(models.Area).count()
        days_count = db.query(models.FestivalDay).count()
        print(f"ℹ️  {count} Bereiche und {days_count} Festivaltage in der DB")
    finally:
        db.close()


if __name__ == "__main__":
    main()
