"""End-to-end smoke test: spawns uvicorn, hits every endpoint, reports issues.

Läuft komplett in einem Prozess, damit der Server nicht zwischen Tool-Calls stirbt.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).parent
BASE = "http://127.0.0.1:8766"

results: list[tuple[str, bool, str]] = []

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def post_form(client: httpx.Client, path: str, pairs: list[tuple[str, str]], **kwargs) -> httpx.Response:
    """POST application/x-www-form-urlencoded mit erlaubten Duplikat-Keys."""
    body = urlencode(pairs, doseq=False)
    return client.post(path, content=body, headers=FORM_HEADERS, **kwargs)


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    marker = "✓" if ok else "✗"
    print(f"  {marker} {label}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    # Server starten
    env = os.environ.copy()
    env.setdefault("DATABASE_URL", "sqlite:///./chimaera_smoke.db")
    env.setdefault("ADMIN_USERNAME", "admin")
    env.setdefault("ADMIN_PASSWORD", "smoketest-pw")
    env.setdefault("SECRET_KEY", "smoketest-secret-key-xxxxxxxxxxxxxxxxxxxx")
    # Default: Schichtplan freigegeben — die "Locked"-Variante testen wir
    # separat mit einem zweiten Server-Prozess weiter unten.
    env.setdefault("SHIFT_SIGNUP_OPEN", "true")

    # Fresh DB
    db_file = ROOT / "chimaera_smoke.db"
    db_file.unlink(missing_ok=True)

    print("=== Init DB ===")
    init = subprocess.run(
        [sys.executable, "init_db.py", "--with-days"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    print(init.stdout, end="")
    if init.returncode != 0:
        print("init_db FAILED:", init.stderr)
        return 1

    print("=== Starting uvicorn ===")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8766", "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Warte auf Server
        client = httpx.Client(base_url=BASE, timeout=10)
        for _ in range(30):
            try:
                if client.get("/").status_code:
                    break
            except httpx.ConnectError:
                time.sleep(0.3)
        else:
            print("Server kam nicht hoch.")
            return 1

        print("\n=== Public Flows ===")

        # GET /
        r = client.get("/")
        check("GET /", r.status_code == 200,
              f"status={r.status_code}, day_checkboxes={r.text.count('availability_day_ids')}")
        has_form = 'name="first_name"' in r.text
        check("  form rendered", has_form)

        # POST /register mit gültigen Daten
        import re
        # Tag- und Area-IDs aus DB ziehen
        from sqlalchemy import create_engine, text as sa_text
        eng = create_engine(env["DATABASE_URL"])
        with eng.connect() as conn:
            day_ids = [row[0] for row in conn.execute(sa_text("SELECT id FROM festival_days ORDER BY id")).fetchall()]
            area_ids = [row[0] for row in conn.execute(sa_text("SELECT id FROM areas ORDER BY id LIMIT 3")).fetchall()]

        good_data = [
            ("first_name", "Anna"),
            ("last_name", "Beispiel"),
            ("email", "anna@example.org"),
            ("phone", "0123456789"),
            ("date_of_birth", "1995-06-15"),
            ("iban", "DE89 3704 0044 0532 0130 00"),
            ("been_here_before", "no"),
            ("is_adult_confirmed", "on"),
            ("accepted_no_guarantee", "on"),
            ("password", "anna-password-1"),
            ("password_confirm", "anna-password-1"),
        ]
        for did in day_ids[:2]:
            good_data.append(("availability_day_ids", str(did)))
        good_data.append((f"area_rank_{area_ids[0]}", "1"))
        good_data.append((f"area_rank_{area_ids[1]}", "2"))

        r = post_form(client, "/register", good_data)
        check("POST /register (valid)", r.status_code == 200 and "Danke" in r.text,
              f"status={r.status_code}, success_visible={'Danke' in r.text}")

        # POST /register Duplikat-Email
        r = post_form(client, "/register", good_data)
        check("POST /register (duplicate email)", r.status_code == 400 and "existiert bereits" in r.text,
              f"status={r.status_code}")

        # POST /register zu jung
        young_data = list(good_data)
        young_data = [(k, v) for (k, v) in young_data if k not in ("email", "date_of_birth")]
        young_data.append(("email", "young@example.org"))
        young_data.append(("date_of_birth", "2015-01-01"))
        r = post_form(client, "/register", young_data)
        check("POST /register (too young)", r.status_code == 400 and "18" in r.text,
              f"status={r.status_code}")

        # POST /register ohne IBAN UND ohne PayPal
        no_payment_data = [
            ("first_name", "Max"), ("last_name", "Test"),
            ("email", "max@example.org"), ("date_of_birth", "1990-01-01"),
            ("been_here_before", "no"),
            ("is_adult_confirmed", "on"), ("accepted_no_guarantee", "on"),
            ("availability_day_ids", str(day_ids[0])),
            ("password", "test-pw-1234"), ("password_confirm", "test-pw-1234"),
        ]
        r = post_form(client, "/register", no_payment_data)
        check("POST /register (no IBAN / no PayPal)", r.status_code == 400 and "IBAN oder dein PayPal" in r.text)

        # IBAN-Format-Check
        bad_iban_data = [(k, v) for (k, v) in good_data if k != "email"]
        bad_iban_data.append(("email", "badiban@example.org"))
        bad_iban_data = [(k, v) for (k, v) in bad_iban_data if k != "iban"]
        bad_iban_data.append(("iban", "DE00000000000000000000"))  # falsche Prüfziffer
        r = post_form(client, "/register", bad_iban_data)
        check("POST /register (bad IBAN checksum)", r.status_code == 400 and "IBAN" in r.text)

        # POST /register ohne Tag
        no_day_data = list(good_data)
        no_day_data = [(k, v) for (k, v) in no_day_data if k != "availability_day_ids" and k != "email"]
        no_day_data.append(("email", "nodays@example.org"))
        r = post_form(client, "/register", no_day_data)
        check("POST /register (no day)", r.status_code == 400)

        print("\n=== Admin Flow ===")

        # GET /admin (ohne Login → Redirect)
        r = client.get("/admin", follow_redirects=False)
        check("GET /admin (unauth)", r.status_code == 303, f"status={r.status_code}, location={r.headers.get('location', '')}")

        # Falsches Passwort
        r = client.post("/admin/login", data={"username": "admin", "password": "falsch"})
        check("POST /admin/login (wrong pw)", r.status_code == 401)

        # Richtiges Passwort
        r = client.post("/admin/login", data={"username": "admin", "password": "smoketest-pw"}, follow_redirects=False)
        check("POST /admin/login (correct)", r.status_code == 303 and "chimaera_session" in r.headers.get("set-cookie", ""))

        # Alle Admin-Seiten
        for path in ("/admin", "/admin/helpers", "/admin/shifts", "/admin/mail", "/admin/config"):
            r = client.get(path)
            check(f"GET {path}", r.status_code == 200, f"status={r.status_code}")

        # Helper-Detail
        with eng.connect() as conn:
            helper_id = conn.execute(sa_text("SELECT id FROM helpers WHERE email='anna@example.org'")).scalar()
        r = client.get(f"/admin/helpers/{helper_id}")
        check(f"GET /admin/helpers/{helper_id}", r.status_code == 200)
        check("  helper_detail: grouped_roles hack rendered Bar", "Barleitung" in r.text,
              f"has 'Bar':{'Bar' in r.text}, has 'Barleitung':{'Barleitung' in r.text}")

        # CSV-Export
        r = client.get("/admin/export/helpers.csv")
        check("GET /admin/export/helpers.csv", r.status_code == 200 and "anna@example.org" in r.text,
              f"status={r.status_code}, content_type={r.headers.get('content-type', '')}")

        # Schicht anlegen
        with eng.connect() as conn:
            bar_area_id = conn.execute(sa_text("SELECT id FROM areas WHERE name='Bar'")).scalar()
        r = client.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id),
            "day_id": str(day_ids[1]),  # Freitag
            "label": "Hauptbar Schicht 1",
            "start_time": "20:00",
            "end_time": "23:00",
            "capacity": "3",
        }, follow_redirects=False)
        check("POST /admin/shifts/new", r.status_code == 303, f"status={r.status_code}, loc={r.headers.get('location', '')}")

        # Schicht-Detail
        with eng.connect() as conn:
            shift_id = conn.execute(sa_text("SELECT id FROM shifts ORDER BY id DESC LIMIT 1")).scalar()
        r = client.get(f"/admin/shifts/{shift_id}")
        check(f"GET /admin/shifts/{shift_id}", r.status_code == 200)
        check("  shift_detail: Anna als Kandidatin (Bar=Wunsch 1, Fr verfügbar)", "Anna" in r.text,
              f"has Anna:{'Anna' in r.text}")

        # Helfer zuweisen (ohne Rolle)
        r = client.post(f"/admin/shifts/{shift_id}/assign", data={
            "helper_id": str(helper_id), "role_id": ""
        }, follow_redirects=False)
        check(f"POST /admin/shifts/{shift_id}/assign", r.status_code == 303)

        # Verifizieren
        r = client.get(f"/admin/shifts/{shift_id}")
        check("  assignment visible after assign", "Anna Beispiel" in r.text and "1 / 3" in r.text)

        # Shifts list
        r = client.get("/admin/shifts")
        check("GET /admin/shifts (with shifts)", r.status_code == 200 and "Hauptbar Schicht 1" in r.text,
              f"has shift label: {'Hauptbar Schicht 1' in r.text}")

        # Rollen-Zutrauen setzen
        with eng.connect() as conn:
            bar_role_ids = [row[0] for row in conn.execute(
                sa_text("SELECT id FROM roles WHERE area_id=:a"), {"a": bar_area_id}
            ).fetchall()]
        # Save mit section=admin (Status, Notizen, Rollen-Zutrauen)
        form_data = [
            ("section", "admin"),
            ("status", "confirmed"),
            ("admin_notes", "Kann gut mit Gästen"),
        ]
        for rid in bar_role_ids[:2]:
            form_data.append(("trusted_role_ids", str(rid)))
        r = post_form(client, f"/admin/helpers/{helper_id}/save", form_data, follow_redirects=False)
        check("POST save (section=admin: status+trust)", r.status_code == 303)

        # Verifizieren
        r = client.get(f"/admin/helpers/{helper_id}")
        check("  admin section persisted", "Kann gut mit Gästen" in r.text)

        # Save mit section=contact (Name + Telefon edit)
        r = post_form(client, f"/admin/helpers/{helper_id}/save", [
            ("section", "contact"),
            ("first_name", "Anna"), ("last_name", "Beispielsen"),
            ("email", "anna@example.org"),
            ("phone", "0987654321"),
            ("date_of_birth", "1995-06-15"),
            ("iban", "DE1111"), ("paypal", ""), ("notes", ""),
        ], follow_redirects=False)
        check("POST save (section=contact: rename)", r.status_code == 303)
        with eng.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT last_name, phone FROM helpers WHERE id=:i"
            ), {"i": helper_id}).fetchone()
        check("  contact changes persisted", row[0] == "Beispielsen" and row[1] == "0987654321",
              f"got last_name={row[0]}, phone={row[1]}")
        # Status aus admin-section darf unverändert geblieben sein (kein overwrite)
        with eng.connect() as conn:
            status_now = conn.execute(sa_text(
                "SELECT status FROM helpers WHERE id=:i"
            ), {"i": helper_id}).scalar()
        check("  section-contact did not overwrite status", status_now == "confirmed")

        # Save mit section=prefs (Verfügbarkeit + Wunschbereiche editieren)
        # Nehme alle 4 Tage und setze Area_ids[1] auf Rang 1
        prefs_data = [("section", "prefs")]
        for did in day_ids:
            prefs_data.append(("availability_day_ids", str(did)))
        prefs_data.append((f"area_rank_{area_ids[1]}", "1"))  # war vorher area[0]=1, area[1]=2
        r = post_form(client, f"/admin/helpers/{helper_id}/save", prefs_data, follow_redirects=False)
        check("POST save (section=prefs: all days, one area)", r.status_code == 303)
        with eng.connect() as conn:
            n_avail = conn.execute(sa_text(
                "SELECT COUNT(*) FROM availabilities WHERE helper_id=:i"
            ), {"i": helper_id}).scalar()
            n_prefs = conn.execute(sa_text(
                "SELECT COUNT(*) FROM helper_area_preferences WHERE helper_id=:i"
            ), {"i": helper_id}).scalar()
            total_days = conn.execute(sa_text("SELECT COUNT(*) FROM festival_days")).scalar()
        check(f"  all days available ({total_days} total)", n_avail == total_days, f"got {n_avail}")
        # n_prefs zählt nur Bereiche mit explizitem Rang 1 (oder hier 1).
        # Da unser save() aber mit "leer = 5" alle Bereiche speichert, prüfen wir
        # einfach nur, dass der Eintrag mit Rang 1 da ist:
        with eng.connect() as conn:
            top_pref = conn.execute(sa_text(
                "SELECT area_id FROM helper_area_preferences WHERE helper_id=:i AND rank=1"
            ), {"i": helper_id}).scalar()
        check("  rank-1 pref persisted", top_pref == area_ids[1], f"got area_id={top_pref}")

        # Save mit section=pfand
        r = post_form(client, f"/admin/helpers/{helper_id}/save", [
            ("section", "pfand"), ("pfand_paid", "on"),
        ], follow_redirects=False)
        check("POST save (section=pfand: paid)", r.status_code == 303)
        with eng.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT pfand_paid, pfand_paid_at, pfand_returned FROM helpers WHERE id=:i"
            ), {"i": helper_id}).fetchone()
        check("  pfand_paid=True + timestamp set", bool(row[0]) and row[1] is not None)
        check("  pfand_returned still False", not bool(row[2]))

        # Pfand zurückgegeben
        r = post_form(client, f"/admin/helpers/{helper_id}/save", [
            ("section", "pfand"), ("pfand_paid", "on"), ("pfand_returned", "on"),
        ], follow_redirects=False)
        check("POST save (section=pfand: returned)", r.status_code == 303)
        with eng.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT pfand_returned, pfand_returned_at FROM helpers WHERE id=:i"
            ), {"i": helper_id}).fetchone()
        check("  pfand_returned=True + timestamp", bool(row[0]) and row[1] is not None)

        # Un-paid soll returned auch zurücksetzen
        r = post_form(client, f"/admin/helpers/{helper_id}/save", [
            ("section", "pfand"),  # keine Checkboxen = beide off
        ], follow_redirects=False)
        with eng.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT pfand_paid, pfand_returned FROM helpers WHERE id=:i"
            ), {"i": helper_id}).fetchone()
        check("  un-paid resets returned", not bool(row[0]) and not bool(row[1]))

        # Mail page mit Helfern
        r = client.get("/admin/mail")
        check("GET /admin/mail shows helpers", r.status_code == 200 and "anna@example.org" in r.text)

        # Mail-Seite mit echtem Filter (leerer string für "alle")
        r = client.get("/admin/mail?day_id=&area_id=&status=")
        check("GET /admin/mail with empty filter strings (regression)", r.status_code == 200)

        # Und Schichten mit leerem Filter
        r = client.get("/admin/shifts?area_id=&day_id=")
        check("GET /admin/shifts with empty filter strings (regression)", r.status_code == 200)

        # Helfer-Liste mit leerem Filter
        r = client.get("/admin/helpers?day_id=&area_id=&status=&experience=&q=")
        check("GET /admin/helpers with empty filter strings (regression)", r.status_code == 200)

        # CSV-Import: CSV round-trip
        csv_out = client.get("/admin/export/helpers.csv").text
        files = {"file": ("helpers.csv", csv_out.encode("utf-8"), "text/csv")}
        r = client.post("/admin/import/helpers", files=files, follow_redirects=False)
        check("POST /admin/import/helpers (round-trip)", r.status_code == 303,
              f"status={r.status_code}, loc={r.headers.get('location', '')}")

        # Unassign
        r = client.post(f"/admin/shifts/{shift_id}/unassign/{helper_id}", follow_redirects=False)
        check(f"POST /admin/shifts/{shift_id}/unassign/{helper_id}", r.status_code == 303)

        # Logout
        r = client.post("/admin/logout", follow_redirects=False)
        check("POST /admin/logout", r.status_code == 303)

        # Nach Logout sollte /admin wieder redirecten
        client.cookies.clear()
        r = client.get("/admin", follow_redirects=False)
        check("GET /admin (after logout)", r.status_code == 303)

        print("\n=== Helper Auth Flow ===")

        # 1) Neue Helferin Ben registrieren (eigener Client, damit sauber getrennt)
        c_ben = httpx.Client(base_url=BASE, timeout=10)
        ben_data = [
            ("first_name", "Ben"), ("last_name", "Tester"),
            ("email", "ben@example.org"),
            ("date_of_birth", "1992-03-10"),
            ("paypal", "ben@paypal.example"),
            ("been_here_before", "no"),
            ("is_adult_confirmed", "on"), ("accepted_no_guarantee", "on"),
            ("password", "ben-password-1"),
            ("password_confirm", "ben-password-1"),
        ]
        for did in day_ids[:2]:
            ben_data.append(("availability_day_ids", str(did)))
        ben_data.append((f"area_rank_{area_ids[0]}", "1"))
        r = post_form(c_ben, "/register", ben_data)
        check("register Ben (with password, auto-login)", r.status_code == 200 and "Danke" in r.text
              and c_ben.cookies.get("chimaera_helper_session"))

        # GET /me sollte sofort funktionieren
        r = c_ben.get("/me")
        check("GET /me (Ben)", r.status_code == 200 and "Hallo, Ben" in r.text)

        # Logout, dann Login-Formular prüfen
        r = c_ben.post("/logout", follow_redirects=False)
        check("POST /logout", r.status_code == 303)
        c_ben.cookies.clear()
        r = c_ben.get("/me", follow_redirects=False)
        check("GET /me (unauth) redirects", r.status_code == 303)

        # Login mit falschem PW
        r = post_form(c_ben, "/login", [
            ("email", "ben@example.org"), ("password", "falsch"), ("next", "/me"),
        ])
        check("POST /login (wrong pw)", r.status_code == 401 and "stimmt nicht" in r.text)

        # Login mit richtigem PW
        r = post_form(c_ben, "/login", [
            ("email", "ben@example.org"), ("password", "ben-password-1"), ("next", "/me"),
        ], follow_redirects=False)
        check("POST /login (correct)", r.status_code == 303 and c_ben.cookies.get("chimaera_helper_session"))

        # Passwort vergessen-Flow (SMTP aus → generischer Erfolg, kein Enumerations-Leak)
        r = post_form(c_ben, "/forgot", [("email", "ben@example.org")])
        check("POST /forgot (existing email)", r.status_code == 200 and "haben wir gerade einen Reset-Link" in r.text)
        r = post_form(c_ben, "/forgot", [("email", "does-not-exist@example.org")])
        check("POST /forgot (unknown email, no leak)", r.status_code == 200 and "haben wir gerade einen Reset-Link" in r.text)

        # Reset-Token aus DB ziehen
        with eng.connect() as conn:
            ben_token = conn.execute(sa_text(
                "SELECT password_reset_token FROM helpers WHERE email='ben@example.org'"
            )).scalar()
        check("reset token persisted", bool(ben_token))

        # Reset-Formular öffnen und neues Passwort setzen
        r = c_ben.get(f"/reset/{ben_token}")
        check(f"GET /reset/{{token}}", r.status_code == 200 and "Neues Passwort setzen" in r.text)
        r = post_form(c_ben, f"/reset/{ben_token}", [
            ("password", "ben-password-2"), ("password_confirm", "ben-password-2"),
        ], follow_redirects=False)
        check("POST /reset/{token} sets new pw + logs in", r.status_code == 303)

        # Altes PW sollte nicht mehr gehen
        c_ben2 = httpx.Client(base_url=BASE, timeout=10)
        r = post_form(c_ben2, "/login", [
            ("email", "ben@example.org"), ("password", "ben-password-1"), ("next", "/me"),
        ])
        check("old password revoked", r.status_code == 401)

        # Neues PW geht
        r = post_form(c_ben2, "/login", [
            ("email", "ben@example.org"), ("password", "ben-password-2"), ("next", "/me"),
        ], follow_redirects=False)
        check("new password works", r.status_code == 303)
        c_ben2.cookies.clear()

        print("\n=== Schichttausch ===")

        # Admin: Ben einer neuen Schicht zuweisen und Anna auch einer (verschiedenen)
        # Re-login Admin
        c_admin = httpx.Client(base_url=BASE, timeout=10)
        r = post_form(c_admin, "/admin/login", [
            ("username", "admin"), ("password", "smoketest-pw"),
        ], follow_redirects=False)
        assert r.status_code == 303

        # Bens Helper-ID holen
        with eng.connect() as conn:
            ben_id = conn.execute(sa_text("SELECT id FROM helpers WHERE email='ben@example.org'")).scalar()
            anna_id = conn.execute(sa_text("SELECT id FROM helpers WHERE email='anna@example.org'")).scalar()

        # Eine zweite Schicht am Samstag anlegen — NICHT in Bar, weil Bar vom
        # Tausch ausgeschlossen ist. Wir nutzen Einlass für die Board-Tests.
        with eng.connect() as conn:
            einlass_area_id = conn.execute(sa_text("SELECT id FROM areas WHERE name='Einlass'")).scalar()
        r = c_admin.post("/admin/shifts/new", data={
            "area_id": str(einlass_area_id),
            "day_id": str(day_ids[2]),  # Samstag
            "label": "Einlass Sa", "start_time": "20:00", "end_time": "23:00", "capacity": "2",
        }, follow_redirects=False)
        with eng.connect() as conn:
            shift2_id = conn.execute(sa_text("SELECT id FROM shifts ORDER BY id DESC LIMIT 1")).scalar()

        # Ben der Sa-Schicht zuweisen (Ben ist Do+Fr verfügbar → Sa wäre neu;
        # Assign funktioniert admin-seitig trotzdem). Wir setzen Ben als availability.
        # Für den Test ergänzen wir Ben's availability an Sa in der DB direkt.
        with eng.begin() as conn:
            conn.execute(sa_text(
                "INSERT OR IGNORE INTO availabilities (helper_id, day_id) VALUES (:h, :d)"
            ), {"h": ben_id, "d": day_ids[2]})
            conn.execute(sa_text(
                "INSERT OR IGNORE INTO helper_area_preferences (helper_id, area_id, rank) VALUES (:h, :a, 1)"
            ), {"h": ben_id, "a": bar_area_id})
        r = c_admin.post(f"/admin/shifts/{shift2_id}/assign", data={
            "helper_id": str(ben_id), "role_id": "",
        }, follow_redirects=False)
        check("admin assigns Ben to shift2", r.status_code == 303)

        # Ben's assignment-ID holen
        with eng.connect() as conn:
            ben_assign_id = conn.execute(sa_text(
                "SELECT id FROM shift_assignments WHERE helper_id=:h AND shift_id=:s"
            ), {"h": ben_id, "s": shift2_id}).scalar()

        # Ben logged in, stellt Schicht aufs Board.
        # Neues Modell: Ben erlaubt hier die reine Übernahme (allow_giveaway),
        # damit Anna ohne Gegenschicht übernehmen kann. want_type=day mit
        # irgendeinem Tag ist Pflichtfeld, spielt aber bei giveaway keine Rolle
        # fürs Übernehmen.
        r = post_form(c_ben2, "/login", [
            ("email", "ben@example.org"), ("password", "ben-password-2"), ("next", "/me"),
        ], follow_redirects=True)
        with eng.connect() as conn:
            some_day = conn.execute(sa_text("SELECT id FROM festival_days ORDER BY sort_order LIMIT 1")).scalar()
        r = post_form(c_ben2, f"/me/assignments/{ben_assign_id}/offer",
                      [("message", "Kann leider nicht mehr."),
                       ("want_type", "day"), ("wanted_day_id", str(some_day)),
                       ("allow_giveaway", "on")], follow_redirects=False)
        check("Ben offers shift on board", r.status_code == 303)

        # Board anschauen (als Ben selbst → eigenes Angebot im 'own' Abschnitt)
        r = c_ben2.get("/board")
        check("GET /board (Ben sees own offer)", r.status_code == 200 and "Deine eigenen Angebote" in r.text)

        # Anna logged in → sieht Ben's Angebot im foreign-Abschnitt
        # Anna muss erst Passwort per Reset setzen (sie hatte noch keines aus dem Urtest).
        # Admin erzeugt ihr einen Reset-Link.
        r = c_admin.post(f"/admin/helpers/{anna_id}/reset-link", follow_redirects=True)
        check("admin generates anna's reset link", r.status_code == 200 and "Neuer Reset-Link" in r.text)

        # Admin schickt Verifikations-Mail erneut. SMTP ist im Test-Env nicht
        # konfiguriert → Endpoint soll info-Flash mit Link zurückgeben, einen
        # frischen Token in der DB anlegen, und 200 (nicht crashen).
        with eng.connect() as conn:
            old_verify_token = conn.execute(sa_text(
                "SELECT email_verification_token FROM helpers WHERE id=:i"
            ), {"i": anna_id}).scalar()
        r = c_admin.post(f"/admin/helpers/{anna_id}/resend-verify", follow_redirects=True)
        check("admin resends verify mail (no SMTP)",
              r.status_code == 200 and "SMTP nicht konfiguriert" in r.text and "/verify/" in r.text)
        with eng.connect() as conn:
            new_verify_token = conn.execute(sa_text(
                "SELECT email_verification_token FROM helpers WHERE id=:i"
            ), {"i": anna_id}).scalar()
        check("  fresh verification token persisted",
              new_verify_token is not None and new_verify_token != old_verify_token)

        # Anna verifiziert über den neuen Token
        from urllib.parse import urlparse
        # nicht über den HTML-Body parsen, einfach DB-Token nehmen
        r = httpx.Client(base_url=BASE, timeout=10).get(f"/verify/{new_verify_token}")
        check("anna verifies with new admin-resent token", r.status_code == 200 and "bestätigt" in r.text)

        # Erneuter Resend-Versuch sollte jetzt info-Flash "bereits verifiziert" zeigen
        r = c_admin.post(f"/admin/helpers/{anna_id}/resend-verify", follow_redirects=True)
        check("admin resend on verified helper is no-op",
              r.status_code == 200 and "bereits verifiziert" in r.text)

        with eng.connect() as conn:
            anna_token = conn.execute(sa_text(
                "SELECT password_reset_token FROM helpers WHERE id=:i"
            ), {"i": anna_id}).scalar()
        c_anna = httpx.Client(base_url=BASE, timeout=10)
        r = post_form(c_anna, f"/reset/{anna_token}", [
            ("password", "anna-pw-xxxx"), ("password_confirm", "anna-pw-xxxx"),
        ], follow_redirects=False)
        check("anna sets password via admin-generated link", r.status_code == 303)

        # Anna sieht Board
        r = c_anna.get("/board")
        check("GET /board (Anna sees Ben's offer)", r.status_code == 200 and "Ben Tester" in r.text)

        # Anna übernimmt
        with eng.connect() as conn:
            offer_id = conn.execute(sa_text(
                "SELECT id FROM shift_swap_offers WHERE assignment_id=:a AND status='open'"
            ), {"a": ben_assign_id}).scalar()
        r = c_anna.post(f"/board/{offer_id}/take",
                        data={"give_assignment_id": "giveaway"}, follow_redirects=False)
        check("Anna takes Ben's board offer", r.status_code == 303)

        # Assignment gehört jetzt Anna
        with eng.connect() as conn:
            new_owner = conn.execute(sa_text(
                "SELECT helper_id FROM shift_assignments WHERE id=:i"
            ), {"i": ben_assign_id}).scalar()
            offer_status = conn.execute(sa_text(
                "SELECT status FROM shift_swap_offers WHERE id=:i"
            ), {"i": offer_id}).scalar()
        check("assignment transferred to Anna", new_owner == anna_id, f"owner={new_owner}, expected={anna_id}")
        check("offer marked taken", offer_status == "taken")

        print("\n=== Direkte Swap-Anfrage ===")
        # Anna hatte Fr im Admin-Flow; sie wurde dort wieder unassigned.
        # Vor dem Direct-Swap-Test weisen wir ihr die Fr-Schicht erneut zu.
        with eng.connect() as conn:
            anna_fri_assign = conn.execute(sa_text(
                "SELECT id FROM shift_assignments WHERE helper_id=:h AND shift_id=:s"
            ), {"h": anna_id, "s": shift_id}).scalar()
        if not anna_fri_assign:
            c_admin.post(f"/admin/shifts/{shift_id}/assign", data={
                "helper_id": str(anna_id), "role_id": "",
            }, follow_redirects=False)
            with eng.connect() as conn:
                anna_fri_assign = conn.execute(sa_text(
                    "SELECT id FROM shift_assignments WHERE helper_id=:h AND shift_id=:s"
                ), {"h": anna_id, "s": shift_id}).scalar()

        # Bad-Email-Test mit gültiger Assignment-ID (Anna's Fr-Schicht)
        r = post_form(c_anna, f"/me/assignments/{anna_fri_assign}/swap", [
            ("target_email", "wrong-email-that-does-not-exist@example.org"),
            ("message", "?"),
        ])
        check("swap form rejects unknown email", r.status_code == 400 and "nicht als Helfer" in r.text)

        r = post_form(c_anna, f"/me/assignments/{anna_fri_assign}/swap", [
            ("target_email", "ben@example.org"),
            ("message", "Magst du?"),
        ], follow_redirects=False)
        check("Anna sends swap request to Ben", r.status_code == 303)

        # Ben sieht sie auf /me
        r = c_ben2.get("/me")
        check("Ben sees incoming request", r.status_code == 200 and "Anna" in r.text and "Magst du?" in r.text)

        # Ben lehnt ab
        with eng.connect() as conn:
            req_id = conn.execute(sa_text(
                "SELECT id FROM shift_swap_requests WHERE to_helper_id=:b AND status='pending' ORDER BY id DESC LIMIT 1"
            ), {"b": ben_id}).scalar()
        r = c_ben2.post(f"/me/swap-requests/{req_id}/decline", follow_redirects=False)
        check("Ben declines", r.status_code == 303)
        with eng.connect() as conn:
            st = conn.execute(sa_text(
                "SELECT status FROM shift_swap_requests WHERE id=:i"
            ), {"i": req_id}).scalar()
        check("request marked declined", st == "declined")

        # Anna versucht nochmal — Ben akzeptiert
        r = post_form(c_anna, f"/me/assignments/{anna_fri_assign}/swap", [
            ("target_email", "ben@example.org"), ("message", "Bitte!"),
        ], follow_redirects=False)
        with eng.connect() as conn:
            req2_id = conn.execute(sa_text(
                "SELECT id FROM shift_swap_requests WHERE to_helper_id=:b AND status='pending' ORDER BY id DESC LIMIT 1"
            ), {"b": ben_id}).scalar()
        r = c_ben2.post(f"/me/swap-requests/{req2_id}/accept", follow_redirects=False)
        check("Ben accepts swap", r.status_code == 303)
        with eng.connect() as conn:
            new_owner2 = conn.execute(sa_text(
                "SELECT helper_id FROM shift_assignments WHERE id=:i"
            ), {"i": anna_fri_assign}).scalar()
        check("swap: shift now belongs to Ben", new_owner2 == ben_id, f"owner={new_owner2}, expected={ben_id}")

        # === Echter 1:1-Tausch (mit Gegenschicht) ===
        # Self-contained: eigene frische Schichten, stört keine anderen Tests.
        print("\n=== 1:1-Tausch (Board) ===")
        c_admin.post("/admin/shifts/new", data={
            "area_id": str(einlass_area_id), "day_id": str(day_ids[1]),  # Fr
            "label": "Swap-Ben-Fr", "start_time": "09:00", "end_time": "12:00", "capacity": "1",
        }, follow_redirects=False)
        so_day = day_ids[3] if len(day_ids) > 3 else day_ids[2]
        c_admin.post("/admin/shifts/new", data={
            "area_id": str(einlass_area_id), "day_id": str(so_day),
            "label": "Swap-Anna-So", "start_time": "09:00", "end_time": "12:00", "capacity": "1",
        }, follow_redirects=False)
        with eng.connect() as conn:
            sb = conn.execute(sa_text("SELECT id FROM shifts WHERE label='Swap-Ben-Fr'")).scalar()
            sa_ = conn.execute(sa_text("SELECT id FROM shifts WHERE label='Swap-Anna-So'")).scalar()
            swap_day = conn.execute(sa_text("SELECT day_id FROM shifts WHERE id=:i"), {"i": sa_}).scalar()
        c_admin.post(f"/admin/shifts/{sb}/assign", data={"helper_id": str(ben_id), "role_id": ""}, follow_redirects=False)
        c_admin.post(f"/admin/shifts/{sa_}/assign", data={"helper_id": str(anna_id), "role_id": ""}, follow_redirects=False)
        with eng.connect() as conn:
            ben_swap_assign = conn.execute(sa_text(
                "SELECT id FROM shift_assignments WHERE helper_id=:h AND shift_id=:s"), {"h": ben_id, "s": sb}).scalar()
            anna_swap_assign = conn.execute(sa_text(
                "SELECT id FROM shift_assignments WHERE helper_id=:h AND shift_id=:s"), {"h": anna_id, "s": sa_}).scalar()

        r = post_form(c_ben2, f"/me/assignments/{ben_swap_assign}/offer",
                      [("want_type", "day"), ("wanted_day_id", str(swap_day))], follow_redirects=False)
        check("Ben offers with day-preference (no giveaway)", r.status_code == 303)
        with eng.connect() as conn:
            swap_offer_id = conn.execute(sa_text(
                "SELECT id FROM shift_swap_offers WHERE assignment_id=:a AND status='open'"),
                {"a": ben_swap_assign}).scalar()
        check("  1:1 offer created", swap_offer_id is not None)

        r = c_anna.get("/board")
        check("Anna sees 1:1 offer with Tauschen button", "Tauschen" in r.text)

        r = c_anna.post(f"/board/{swap_offer_id}/take",
                        data={"give_assignment_id": str(anna_swap_assign)}, follow_redirects=False)
        check("Anna performs 1:1 swap", r.status_code == 303 and "/me?taken=1" in r.headers.get("location", ""))
        with eng.connect() as conn:
            ben_fr_owner = conn.execute(sa_text("SELECT helper_id FROM shift_assignments WHERE id=:i"), {"i": ben_swap_assign}).scalar()
            anna_so_owner = conn.execute(sa_text("SELECT helper_id FROM shift_assignments WHERE id=:i"), {"i": anna_swap_assign}).scalar()
            taken_with = conn.execute(sa_text("SELECT taken_with_assignment_id FROM shift_swap_offers WHERE id=:i"), {"i": swap_offer_id}).scalar()
        check("  Ben's shift now Anna's", ben_fr_owner == anna_id, f"got {ben_fr_owner}")
        check("  Anna's shift now Ben's", anna_so_owner == ben_id, f"got {anna_so_owner}")
        check("  swap recorded taken_with", taken_with == anna_swap_assign)

        # Aufräumen: die 1:1-Test-Schichten wieder löschen, damit die dadurch
        # entstandenen Zuweisungen (Ben hat jetzt eine So-Schicht) spätere
        # Self-Signup-Tests nicht mit Zeitkonflikten stören.
        c_admin.post(f"/admin/shifts/{sb}/delete", follow_redirects=False)
        c_admin.post(f"/admin/shifts/{sa_}/delete", follow_redirects=False)

        # === Bar ist vom Tausch ausgeschlossen ===
        print("\n=== Bar-Ausschluss ===")
        c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id), "day_id": str(day_ids[1]),
            "label": "Bar-NoSwap", "start_time": "18:00", "end_time": "22:00", "capacity": "1",
        }, follow_redirects=False)
        with eng.connect() as conn:
            bar_shift = conn.execute(sa_text("SELECT id FROM shifts WHERE label='Bar-NoSwap'")).scalar()
        # Zuweisung direkt in der DB (robuster als über Admin-UI, unabhängig
        # von Bens Verfügbarkeit/Kapazität).
        with eng.begin() as conn:
            conn.execute(sa_text(
                "INSERT INTO shift_assignments (shift_id, helper_id) VALUES (:s, :h)"
            ), {"s": bar_shift, "h": ben_id})
            bar_assign = conn.execute(sa_text(
                "SELECT id FROM shift_assignments WHERE helper_id=:h AND shift_id=:s"), {"h": ben_id, "s": bar_shift}).scalar()
        r = c_ben2.get(f"/me/assignments/{bar_assign}/offer", follow_redirects=False)
        check("Bar shift can't be offered (form redirects)",
              r.status_code == 303 and "area_excluded" in r.headers.get("location", ""))
        r = c_ben2.post(f"/me/assignments/{bar_assign}/offer",
                        data={"want_type": "day", "wanted_day_id": str(day_ids[1]), "allow_giveaway": "on"},
                        follow_redirects=False)
        # Kein OFFENES Offer darf für die Bar-Zuweisung entstehen. (Wir filtern
        # auf status='open', weil SQLite freigegebene Assignment-IDs recyceln
        # kann und alte, bereits abgeschlossene Offers dieselbe ID referenzieren.)
        with eng.connect() as conn:
            bar_offer = conn.execute(sa_text(
                "SELECT id FROM shift_swap_offers WHERE assignment_id=:a AND status='open'"),
                {"a": bar_assign}).scalar()
        check("Bar shift POST offer blocked (no offer created)", bar_offer is None)

        # Bar-Test-Schicht wieder löschen
        c_admin.post(f"/admin/shifts/{bar_shift}/delete", follow_redirects=False)

        print("\n=== Neue Features ===")

        # --- 0. Admin-Schichttausch (v.a. für Bar) ---
        # Zwei frische Bar-Schichten, je einem Helfer zugewiesen, dann tauschen.
        c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id), "day_id": str(day_ids[1]),
            "label": "AdmSwap-A", "start_time": "08:00", "end_time": "10:00", "capacity": "1",
        }, follow_redirects=False)
        c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id), "day_id": str(day_ids[2]),
            "label": "AdmSwap-B", "start_time": "08:00", "end_time": "10:00", "capacity": "1",
        }, follow_redirects=False)
        with eng.connect() as conn:
            adm_a = conn.execute(sa_text("SELECT id FROM shifts WHERE label='AdmSwap-A'")).scalar()
            adm_b = conn.execute(sa_text("SELECT id FROM shifts WHERE label='AdmSwap-B'")).scalar()
        with eng.begin() as conn:
            conn.execute(sa_text("INSERT INTO shift_assignments (shift_id, helper_id) VALUES (:s,:h)"), {"s": adm_a, "h": ben_id})
            conn.execute(sa_text("INSERT INTO shift_assignments (shift_id, helper_id) VALUES (:s,:h)"), {"s": adm_b, "h": anna_id})
            adm_assign_a = conn.execute(sa_text("SELECT id FROM shift_assignments WHERE shift_id=:s AND helper_id=:h"), {"s": adm_a, "h": ben_id}).scalar()
            adm_assign_b = conn.execute(sa_text("SELECT id FROM shift_assignments WHERE shift_id=:s AND helper_id=:h"), {"s": adm_b, "h": anna_id}).scalar()

        # Swap-Seite lädt
        r = c_admin.get("/admin/swap")
        check("admin swap page loads", r.status_code == 200 and "Schichten tauschen" in r.text)

        # Tausch durchführen
        r = c_admin.post("/admin/swap", data={
            "assignment_a": str(adm_assign_a), "assignment_b": str(adm_assign_b),
        }, follow_redirects=False)
        check("admin swap performed", r.status_code == 303 and "flash=swapped" in r.headers.get("location", ""))
        with eng.connect() as conn:
            # Ben's Assignment zeigt jetzt auf B's Schicht (Schichten wechseln
            # via helper_id-Tausch: adm_assign_a gehört jetzt Anna)
            owner_a = conn.execute(sa_text("SELECT helper_id FROM shift_assignments WHERE id=:i"), {"i": adm_assign_a}).scalar()
            owner_b = conn.execute(sa_text("SELECT helper_id FROM shift_assignments WHERE id=:i"), {"i": adm_assign_b}).scalar()
        check("  helpers swapped", owner_a == anna_id and owner_b == ben_id,
              f"a_owner={owner_a}, b_owner={owner_b}")

        # Tausch mit derselben Person auf beiden Seiten wird abgelehnt
        r = c_admin.post("/admin/swap", data={
            "assignment_a": str(adm_assign_a), "assignment_b": str(adm_assign_a),
        }, follow_redirects=False)
        check("admin swap rejects identical assignment", "flash=missing" in r.headers.get("location", ""))

        # Aufräumen
        c_admin.post(f"/admin/shifts/{adm_a}/delete", follow_redirects=False)
        c_admin.post(f"/admin/shifts/{adm_b}/delete", follow_redirects=False)


        r = post_form(c_ben2, "/me/shift-preference", [("wants_only_one_shift", "on")],
                      follow_redirects=False)
        check("Ben toggles wants_only_one_shift on", r.status_code == 303)
        with eng.connect() as conn:
            v = conn.execute(sa_text(
                "SELECT wants_only_one_shift FROM helpers WHERE id=:i"
            ), {"i": ben_id}).scalar()
        check("  flag persisted = True", bool(v))
        # Toggle aus
        r = post_form(c_ben2, "/me/shift-preference", [], follow_redirects=False)
        check("Ben toggles wants_only_one_shift off", r.status_code == 303)
        with eng.connect() as conn:
            v = conn.execute(sa_text(
                "SELECT wants_only_one_shift FROM helpers WHERE id=:i"
            ), {"i": ben_id}).scalar()
        check("  flag persisted = False", not bool(v))

        # --- 2. Selbst-Eintragen Schichtplan ---
        # Admin legt neue Schicht im Bereich Bar an, an Sa (day_ids[1]).
        # Ben hat Bar als Wunsch 1 (oben gesetzt), aber wir haben durch reset womöglich
        # andere Preferences. Stellen wir sicher, dass Bar Wunschbereich ist:
        with eng.begin() as conn:
            conn.execute(sa_text(
                "INSERT OR IGNORE INTO helper_area_preferences (helper_id, area_id, rank) "
                "VALUES (:h, :a, 1)"
            ), {"h": ben_id, "a": bar_area_id})

        r = c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id),
            "day_id": str(day_ids[1]),  # Sa
            "label": "Selfsign-Bar 1",
            "start_time": "14:00", "end_time": "17:00", "capacity": "1",
        }, follow_redirects=False)
        with eng.connect() as conn:
            selfsign_shift_id = conn.execute(sa_text(
                "SELECT id FROM shifts WHERE label='Selfsign-Bar 1'"
            )).scalar()

        # Ben sieht die Schicht
        r = c_ben2.get("/schichten")
        check("GET /schichten as Ben", r.status_code == 200 and "Selfsign-Bar 1" in r.text)

        # Ben trägt sich ein
        r = c_ben2.post(f"/schichten/{selfsign_shift_id}/buchen", follow_redirects=False)
        check("Ben books selfsign shift", r.status_code == 303 and "flash=taken" in r.headers.get("location",""))
        with eng.connect() as conn:
            n = conn.execute(sa_text(
                "SELECT COUNT(*) FROM shift_assignments WHERE shift_id=:s AND helper_id=:h"
            ), {"s": selfsign_shift_id, "h": ben_id}).scalar()
        check("  assignment created", n == 1)

        # Versuch: nochmal eintragen → already
        r = c_ben2.post(f"/schichten/{selfsign_shift_id}/buchen", follow_redirects=False)
        check("Ben can't double-book same shift", "flash=already" in r.headers.get("location",""))

        # Race-Test: Schicht ist jetzt voll (Kapazität 1). Anna versucht.
        # Anna braucht Bar als Wunschbereich.
        with eng.begin() as conn:
            conn.execute(sa_text(
                "INSERT OR IGNORE INTO helper_area_preferences (helper_id, area_id, rank) "
                "VALUES (:h, :a, 1)"
            ), {"h": anna_id, "a": bar_area_id})
        r = c_anna.post(f"/schichten/{selfsign_shift_id}/buchen", follow_redirects=False)
        check("Anna gets 'race' on full shift", "flash=race" in r.headers.get("location",""))

        # Zeitkonflikt-Test: zweite Schicht zur GLEICHEN Zeit anlegen, Ben versucht
        r = c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id),
            "day_id": str(day_ids[1]),
            "label": "Conflict-Bar",
            "start_time": "15:00", "end_time": "16:00",
            "capacity": "1",
        }, follow_redirects=False)
        with eng.connect() as conn:
            conflict_id = conn.execute(sa_text(
                "SELECT id FROM shifts WHERE label='Conflict-Bar'"
            )).scalar()
        r = c_ben2.post(f"/schichten/{conflict_id}/buchen", follow_redirects=False)
        check("Ben blocked by time conflict", "flash=conflict" in r.headers.get("location",""))

        # "Nur eine Schicht"-Limit-Test
        post_form(c_ben2, "/me/shift-preference", [("wants_only_one_shift", "on")],
                  follow_redirects=False)
        # Ben hat schon mind. eine Zuweisung → versucht Conflict-Bar einzubuchen → max_reached vorrangig vor conflict
        # (Reihenfolge: pref_area check, dann max_reached, dann already, dann conflict)
        # Wir nehmen eine andere Schicht, die kein Konflikt wäre:
        r = c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id),
            "day_id": str(day_ids[2]),  # So
            "label": "MaxLimit-Bar",
            "start_time": "10:00", "end_time": "12:00",
            "capacity": "5",
        }, follow_redirects=False)
        with eng.connect() as conn:
            max_shift_id = conn.execute(sa_text(
                "SELECT id FROM shifts WHERE label='MaxLimit-Bar'"
            )).scalar()
        r = c_ben2.post(f"/schichten/{max_shift_id}/buchen", follow_redirects=False)
        check("Ben blocked by max_reached", "flash=max_reached" in r.headers.get("location",""))

        # Toggle off, dann sollte's klappen
        post_form(c_ben2, "/me/shift-preference", [], follow_redirects=False)
        r = c_ben2.post(f"/schichten/{max_shift_id}/buchen", follow_redirects=False)
        check("Ben books after max toggled off", "flash=taken" in r.headers.get("location",""))

        # Wunschbereichs-Check: Schicht in nicht gewünschtem Bereich
        # Anna will mal nicht in den Einlass — wir suchen einen Bereich, in dem
        # Anna NICHT als Wunsch eingetragen ist.
        with eng.connect() as conn:
            unwanted_area_id = conn.execute(sa_text(
                "SELECT a.id FROM areas a WHERE a.id NOT IN ("
                "  SELECT area_id FROM helper_area_preferences WHERE helper_id=:h"
                ") LIMIT 1"
            ), {"h": anna_id}).scalar()
        if unwanted_area_id:
            r = c_admin.post("/admin/shifts/new", data={
                "area_id": str(unwanted_area_id),
                "day_id": str(day_ids[2]),
                "label": "NotWanted-Shift",
                "start_time": "16:00", "end_time": "18:00", "capacity": "1",
            }, follow_redirects=False)
            with eng.connect() as conn:
                notw_id = conn.execute(sa_text(
                    "SELECT id FROM shifts WHERE label='NotWanted-Shift'"
                )).scalar()
            r = c_anna.post(f"/schichten/{notw_id}/buchen", follow_redirects=False)
            check("Anna blocked from non-wishlist area", "flash=not_wanted_area" in r.headers.get("location",""))

        # --- 3. Admin legt manuell Helfer:in an ---
        r = c_admin.get("/admin/helpers/new")
        check("GET /admin/helpers/new", r.status_code == 200 and "Helfer:in manuell anlegen" in r.text)

        # Validation: Email Pflicht
        r = c_admin.post("/admin/helpers/new",
                         data={"first_name": "Walk", "last_name": "In"})
        check("admin/helpers/new requires email", r.status_code == 400)

        # Erfolg
        r = c_admin.post("/admin/helpers/new", data={
            "first_name": "Walk", "last_name": "In",
            "email": "walkin@example.org",
            "phone": "0123",
        }, follow_redirects=False)
        check("admin creates Walk In", r.status_code == 303)
        with eng.connect() as conn:
            walkin = conn.execute(sa_text(
                "SELECT id, email_verified_at, is_adult_confirmed FROM helpers WHERE email='walkin@example.org'"
            )).fetchone()
        check("  walkin persisted, email already verified, adult confirmed",
              walkin is not None and walkin[1] is not None and bool(walkin[2]))

        # Doppelte Email blockiert
        r = c_admin.post("/admin/helpers/new", data={
            "first_name": "Other", "last_name": "Walk",
            "email": "walkin@example.org",
        })
        check("duplicate email rejected", r.status_code == 400 and "existiert bereits" in r.text)

        # Admin-Anlage MIT Passwort → Person kann sich direkt einloggen
        r = c_admin.post("/admin/helpers/new", data={
            "first_name": "Manual", "last_name": "WithPw",
            "email": "manualpw@example.org",
            "password": "manualpw-secret-1",
            # ohne send_verify_email → wird sofort verifiziert
        }, follow_redirects=False)
        check("admin creates manualpw with password", r.status_code == 303)
        c_manual = httpx.Client(base_url=BASE, timeout=10)
        r = post_form(c_manual, "/login", [
            ("email", "manualpw@example.org"), ("password", "manualpw-secret-1"),
        ], follow_redirects=False)
        check("  manualpw can log in immediately", r.status_code == 303)

        # Admin-Anlage mit zu kurzem Passwort
        r = c_admin.post("/admin/helpers/new", data={
            "first_name": "Short", "last_name": "Pw",
            "email": "shortpw@example.org",
            "password": "abc",
        })
        check("short password rejected", r.status_code == 400 and "Zeichen" in r.text)

        # Admin-Anlage MIT send_verify_email → Helfer ist NICHT verifiziert,
        # Token wurde erzeugt
        r = c_admin.post("/admin/helpers/new", data={
            "first_name": "Pending", "last_name": "Verify",
            "email": "pendingverify@example.org",
            "send_verify_email": "on",
        }, follow_redirects=False)
        check("admin creates pendingverify with verify-mail enabled", r.status_code == 303)
        with eng.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT email_verified_at, email_verification_token "
                "FROM helpers WHERE email='pendingverify@example.org'"
            )).fetchone()
        check("  pendingverify NOT verified, token created",
              row is not None and row[0] is None and row[1] is not None)

        # Prio-Sortierung in /schichten: Bar hat Rang 1 für Ben, ein anderer
        # Bereich Rang 2 → Bar-Block sollte VOR dem anderen erscheinen
        # Wir setzen die Ränge EXPLIZIT (frühere Tests könnten andere Bereiche
        # auf Rang 1 gesetzt haben):
        with eng.connect() as conn:
            crew_id = conn.execute(sa_text("SELECT id FROM areas WHERE name='Crew Catering'")).scalar()
        with eng.begin() as conn:
            # Alle Ben-Bereiche zurück auf Rang 5
            conn.execute(sa_text(
                "UPDATE helper_area_preferences SET rank=5 WHERE helper_id=:h"
            ), {"h": ben_id})
            # Bar = 1
            conn.execute(sa_text(
                "UPDATE helper_area_preferences SET rank=1 WHERE helper_id=:h AND area_id=:a"
            ), {"h": ben_id, "a": bar_area_id})
            # Crew Catering = 2
            conn.execute(sa_text(
                "UPDATE helper_area_preferences SET rank=2 WHERE helper_id=:h AND area_id=:a"
            ), {"h": ben_id, "a": crew_id})
        # Schicht in Crew Catering anlegen am gleichen Tag wie Bar (Sa)
        c_admin.post("/admin/shifts/new", data={
            "area_id": str(crew_id), "day_id": str(day_ids[1]),
            "label": "Catering-Sa", "start_time": "12:00", "end_time": "14:00",
            "capacity": "2",
        }, follow_redirects=False)
        r = c_ben2.get("/schichten")
        # Bar-Bereich sollte VOR Crew Catering im HTML stehen (erste Markierung
        # eines Bereichs ist der <h4> mit dem Bereichsnamen)
        bar_pos = r.text.find('>Bar</span>')
        catering_pos = r.text.find('>Crew Catering</span>')
        check("/schichten sorted by priority (Bar before Crew Catering)",
              bar_pos != -1 and catering_pos != -1 and bar_pos < catering_pos,
              f"bar_pos={bar_pos}, catering_pos={catering_pos}")
        check("  prio-1 label visible", "1. Wahl" in r.text)
        check("  prio-2 label visible", "2. Wahl" in r.text)

        # Admin-Filter: Bereich-Filter schließt Prio 5 aus
        # Ben hat Bar=1 (durch obigen UPDATE), Anna hat Bar=5 (wir prüfen
        # gleich; ggf. setzen wir's explizit).
        with eng.connect() as conn:
            anna_bar = conn.execute(sa_text(
                "SELECT rank FROM helper_area_preferences "
                "WHERE helper_id=:h AND area_id=:a"
            ), {"h": anna_id, "a": bar_area_id}).scalar()
        if anna_bar is None:
            with eng.begin() as conn:
                conn.execute(sa_text(
                    "INSERT INTO helper_area_preferences (helper_id, area_id, rank) "
                    "VALUES (:h, :a, 5)"
                ), {"h": anna_id, "a": bar_area_id})
        elif anna_bar != 5:
            with eng.begin() as conn:
                conn.execute(sa_text(
                    "UPDATE helper_area_preferences SET rank=5 "
                    "WHERE helper_id=:h AND area_id=:a"
                ), {"h": anna_id, "a": bar_area_id})

        r = c_admin.get(f"/admin/helpers?area_id={bar_area_id}")
        check("admin filter on Bar excludes Anna (rank 5)",
              r.status_code == 200 and "anna@example.org" not in r.text)
        check("  Bar filter includes Ben (rank 1)", "ben@example.org" in r.text)

        # SHIFT_SIGNUP_OPEN=false-Tests: Wir starten dafür einen ZWEITEN Server
        # auf einem anderen Port mit dem Flag auf false. Die DB ist dieselbe
        # (chimaera_smoke.db), wir nutzen also die schon angelegten Helfer.
        print("\n=== SHIFT_SIGNUP_OPEN=false ===")
        env_locked = env.copy()
        env_locked["SHIFT_SIGNUP_OPEN"] = "false"
        proc_locked = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8767", "--log-level", "warning"],
            cwd=ROOT, env=env_locked,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            BASE_LOCKED = "http://127.0.0.1:8767"
            for _ in range(30):
                try:
                    if httpx.get(BASE_LOCKED + "/", timeout=2).status_code:
                        break
                except httpx.ConnectError:
                    time.sleep(0.3)

            # Ben loggt sich neu ein (sein aktuelles PW ist ben-password-2)
            c_ben_locked = httpx.Client(base_url=BASE_LOCKED, timeout=10)
            r = post_form(c_ben_locked, "/login", [
                ("email", "ben@example.org"), ("password", "ben-password-2"),
            ], follow_redirects=False)
            check("ben logs in (locked server)", r.status_code == 303)

            # Ben sieht Locked-Seite
            r = c_ben_locked.get("/schichten")
            check("ben sees locked page when SHIFT_SIGNUP_OPEN=false",
                  r.status_code == 200 and "Schichtplan kommt in Kürze" in r.text)

            # POST schlägt fehl
            with eng.connect() as conn:
                some_shift_id = conn.execute(sa_text("SELECT id FROM shifts LIMIT 1")).scalar()
            r = c_ben_locked.post(f"/schichten/{some_shift_id}/buchen", follow_redirects=False)
            check("POST /schichten/.../buchen blocked when locked",
                  r.status_code == 303 and "flash=locked" in r.headers.get("location", ""))

            # Admin sieht Vorschau (Admin-Bypass)
            c_admin_locked = httpx.Client(base_url=BASE_LOCKED, timeout=10)
            r = post_form(c_admin_locked, "/admin/login", [
                ("username", "admin"), ("password", "smoketest-pw"),
            ], follow_redirects=False)
            # /schichten verlangt helper-Cookie. Admin-Bypass = wenn AdminCookie auch da ist,
            # umgeht das den Lock. Im selben Client zusätzlich als Helfer einloggen:
            r = post_form(c_admin_locked, "/login", [
                ("email", "ben@example.org"), ("password", "ben-password-2"),
            ], follow_redirects=False)
            r = c_admin_locked.get("/schichten")
            check("admin sees preview banner when locked",
                  r.status_code == 200 and "Admin-Vorschau" in r.text)
        finally:
            try:
                import fcntl
                fcntl.fcntl(proc_locked.stderr.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
                err_chunk = proc_locked.stderr.read() or b""
                if b"Error" in err_chunk or b"Traceback" in err_chunk:
                    print("=== LOCKED SERVER STDERR ===")
                    print(err_chunk.decode(errors="replace")[-2000:])
            except Exception:
                pass
            proc_locked.send_signal(signal.SIGTERM)
            try:
                proc_locked.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc_locked.kill()

        # === Zeitgesteuerte Freischaltung + Preview-Email ===
        print("\n=== Zeit-Freischaltung + Preview ===")
        from datetime import datetime, timezone, timedelta
        # (a) OPEN_AT in der Vergangenheit → sollte offen sein trotz SHIFT_SIGNUP_OPEN=false
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        env_past = env.copy()
        env_past["SHIFT_SIGNUP_OPEN"] = "false"
        env_past["SHIFT_SIGNUP_OPEN_AT"] = past
        proc_past = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8768", "--log-level", "warning"],
            cwd=ROOT, env=env_past, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            BASE_PAST = "http://127.0.0.1:8768"
            for _ in range(30):
                try:
                    if httpx.get(BASE_PAST + "/", timeout=2).status_code:
                        break
                except httpx.ConnectError:
                    time.sleep(0.3)
            c_ben_past = httpx.Client(base_url=BASE_PAST, timeout=10)
            post_form(c_ben_past, "/login", [
                ("email", "ben@example.org"), ("password", "ben-password-2"),
            ], follow_redirects=False)
            r = c_ben_past.get("/schichten")
            check("OPEN_AT in past → schedule is open",
                  r.status_code == 200 and "Schichtplan kommt in Kürze" not in r.text)
        finally:
            proc_past.send_signal(signal.SIGTERM)
            try: proc_past.wait(timeout=5)
            except subprocess.TimeoutExpired: proc_past.kill()

        # (b) OPEN_AT in der Zukunft + Preview-Email → Ben (nicht Preview) sieht
        #     locked, Preview-Nutzer sieht die Schichten
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        env_future = env.copy()
        env_future["SHIFT_SIGNUP_OPEN"] = "false"
        env_future["SHIFT_SIGNUP_OPEN_AT"] = future
        env_future["SHIFT_SIGNUP_PREVIEW_EMAILS"] = "ben@example.org"
        proc_future = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8769", "--log-level", "warning"],
            cwd=ROOT, env=env_future, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            BASE_FUTURE = "http://127.0.0.1:8769"
            for _ in range(30):
                try:
                    if httpx.get(BASE_FUTURE + "/", timeout=2).status_code:
                        break
                except httpx.ConnectError:
                    time.sleep(0.3)
            # Ben ist in der Preview-Liste → sieht Schichten + Vorschau-Banner
            c_ben_fut = httpx.Client(base_url=BASE_FUTURE, timeout=10)
            post_form(c_ben_fut, "/login", [
                ("email", "ben@example.org"), ("password", "ben-password-2"),
            ], follow_redirects=False)
            r = c_ben_fut.get("/schichten")
            check("preview user sees shifts before opening",
                  r.status_code == 200 and "Schichtplan kommt in Kürze" not in r.text
                  and "Test-Zugang" in r.text)
            # Preview-Nutzer darf auch buchen
            with eng.connect() as conn:
                free_shift = conn.execute(sa_text(
                    "SELECT s.id FROM shifts s "
                    "LEFT JOIN shift_assignments sa ON sa.shift_id=s.id "
                    "JOIN helper_area_preferences hap ON hap.area_id=s.area_id "
                    "  AND hap.helper_id=:h AND hap.rank<5 "
                    "GROUP BY s.id, s.capacity HAVING COUNT(sa.id) < s.capacity LIMIT 1"
                ), {"h": ben_id}).scalar()
            if free_shift:
                r = c_ben_fut.post(f"/schichten/{free_shift}/buchen", follow_redirects=False)
                check("preview user can book",
                      "flash=taken" in r.headers.get("location", "")
                      or "flash=already" in r.headers.get("location", ""))

            # Anna ist NICHT in der Preview-Liste → sieht Locked-Seite
            c_anna_fut = httpx.Client(base_url=BASE_FUTURE, timeout=10)
            post_form(c_anna_fut, "/login", [
                ("email", "anna@example.org"), ("password", "anna-pw-xxxx"),
            ], follow_redirects=False)
            r = c_anna_fut.get("/schichten")
            check("non-preview user still sees locked page",
                  r.status_code == 200 and "Schichtplan kommt in Kürze" in r.text)
            check("  locked page shows scheduled opening time",
                  "Freischaltung geplant" in r.text)
        finally:
            proc_future.send_signal(signal.SIGTERM)
            try: proc_future.wait(timeout=5)
            except subprocess.TimeoutExpired: proc_future.kill()



    finally:
        # Server-stderr abgreifen (vor terminate, damit wir nichts verlieren)
        try:
            import fcntl
            err_fd = proc.stderr.fileno()
            fcntl.fcntl(err_fd, fcntl.F_SETFL, os.O_NONBLOCK)
            try:
                err_chunk = proc.stderr.read()
            except Exception:
                err_chunk = b""
            if err_chunk:
                txt = err_chunk.decode(errors="replace")
                # Nur Tracebacks/Errors anzeigen
                rel = "\n".join(l for l in txt.split("\n")
                                if any(k in l for k in ("Error", "Traceback", "  File ", "raise ",
                                                        "Attribute", "Type", "Name")))
                if rel.strip():
                    print("\n=== SERVER STDERR ===")
                    print(rel[-3000:])
        except Exception as exc:
            print("could not capture stderr:", exc)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n=== Summary ===")
    fails = [label for label, ok, _ in results if not ok]
    print(f"{len(results) - len(fails)}/{len(results)} passed")
    if fails:
        print("FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("All green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
