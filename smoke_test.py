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
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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

        # Eine zweite Schicht am Samstag anlegen
        r = c_admin.post("/admin/shifts/new", data={
            "area_id": str(bar_area_id),
            "day_id": str(day_ids[2]),  # Samstag
            "label": "Hauptbar Sa",
            "start_time": "20:00", "end_time": "23:00", "capacity": "2",
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

        # Ben logged in, stellt Schicht aufs Board
        r = post_form(c_ben2, "/login", [
            ("email", "ben@example.org"), ("password", "ben-password-2"), ("next", "/me"),
        ], follow_redirects=True)
        r = post_form(c_ben2, f"/me/assignments/{ben_assign_id}/offer",
                      [("message", "Kann leider nicht mehr.")], follow_redirects=False)
        check("Ben offers shift on board", r.status_code == 303)

        # Board anschauen (als Ben selbst → eigenes Angebot im 'own' Abschnitt)
        r = c_ben2.get("/board")
        check("GET /board (Ben sees own offer)", r.status_code == 200 and "Deine eigenen Angebote" in r.text)

        # Anna logged in → sieht Ben's Angebot im foreign-Abschnitt
        # Anna muss erst Passwort per Reset setzen (sie hatte noch keines aus dem Urtest).
        # Admin erzeugt ihr einen Reset-Link.
        r = c_admin.post(f"/admin/helpers/{anna_id}/reset-link", follow_redirects=True)
        check("admin generates anna's reset link", r.status_code == 200 and "Neuer Reset-Link" in r.text)
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
        r = c_anna.post(f"/board/{offer_id}/take", follow_redirects=False)
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

    finally:
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
