# Chimaera Helfer-Tool

Ein kleines FastAPI-Tool, um Helfer:innen-Anmeldungen, Schichtplanung und
Schicht-Tausch für ein einmal-im-Jahr-Festival zu verwalten. Gebaut mit
FastAPI + SQLAlchemy + Jinja2-Templates + Tailwind. SQLite lokal, Postgres
in Produktion.

## Was das Tool macht

**Für Helfer:innen (öffentlich unter `/`):**
- Anmeldung mit Name, Kontakt, Geburtsdatum (Volljährigkeits-Check),
  Tag-Verfügbarkeit, Wunschbereichen mit Ranking (1. Wahl, 2. Wahl, …),
  IBAN/PayPal für Pfand, Passwort.
- Eigener Login-Bereich `/me`: Schichten einsehen, Passwort ändern.
- **Schichttausch:**
  - Schicht aufs Tausch-Board stellen → andere Helfer:innen können direkt
    übernehmen. Das Anbieten ist die Zustimmung; das Übernehmen auch.
  - Direkt mit einer Freund:in tauschen → Email eingeben, die bekommt eine
    Anfrage und muss bestätigen.
- Passwort vergessen → Reset per Email-Link (gültig 2h). Ohne SMTP kann der
  Admin einen Link manuell erzeugen.

**Für den Admin (ein einzelner Login, Credentials aus ENV):**
- **Dashboard:** Gesamtzahl, unverplant, offene Slots, Tages-/Bereichs-
  verteilung, Pfand-Status (klickbar zur vorgefilterten Liste).
- **Helfer:innen-Liste:** filtern nach Tag, Bereich, Status, Erfahrung,
  **Pfand-Status**, Volltext über Name/Email.
- **Helfer-Detail:** alle Felder editierbar in Panels, die unabhängig
  gespeichert werden (Stammdaten / Verfügbarkeit+Wünsche / Admin-Bereich /
  Pfand). Jedes Panel hat seinen eigenen Speichern-Button — kein
  versehentliches Überschreiben.
- **Rollen-Zutrauen** pro Helfer:in pro Bereich — Schichtplanung zeigt das
  als Hinweis (z.B. Bar → Barleitung, Springer, Tresenkraft, Runner).
- **Schichten:** anlegen (Bereich, Tag, Zeit, Kapazität), Kandidat:innen
  zuweisen (sortiert nach Wunsch-Rang, mit Hinweis auf zugetraute Rollen).
- **Pfand-Tracking:** „Pfand bezahlt" und „Pfand zurückgegeben" pro Helfer,
  mit automatischen Timestamps. Beim Wegnehmen von „bezahlt" wird
  „zurückgegeben" automatisch mit entfernt. Filter + Dashboard-Kennzahlen.
- **Stammdaten:** Festivaltage, Einsatzbereiche, Rollen.
- **Mail-Verteiler:** filtern, Empfängerliste kopieren (BCC im eigenen
  Mailprogramm) oder via SMTP versenden. Platzhalter: `{{Vorname}}`,
  `{{Nachname}}`, `{{FestivalName}}`.
- **CSV-Import/-Export** inkl. Pfand-Spalten.
- **Passwort-Reset-Link erzeugen** für einzelne Helfer:innen (Fallback ohne
  SMTP oder wenn Email nicht ankommt).

## Helfer-Status: was bedeuten die vier Werte?

Die Status-Werte sind **rein dokumentarisch**. Sie steuern keine Logik —
ausser dass du nach ihnen filtern und gezielt Mails schreiben kannst.

| Status | Bedeutung | Wann setzen? |
|---|---|---|
| `registered` | Hat sich selbst angemeldet | Default, passiert automatisch |
| `confirmed` | Admin hat Platz zugesagt | Manuell, nach Admin-Entscheidung |
| `declined` | Admin hat abgesagt | Manuell, wenn mehr Bewerber:innen als Plätze |
| `withdrawn` | Person hat sich selbst zurückgezogen | Manuell, wenn jemand absagt |

Diese Personen tauchen weiterhin in der Kandidat:innen-Liste bei Schichten
auf — wenn du das nicht willst, einfach sagen, das ist eine Zeile Code.

## Lokales Setup

Voraussetzung: Python 3.10+ (getestet mit 3.12).

```bash
# 1. venv anlegen und aktivieren
python3 -m venv .venv
. .venv/bin/activate

# 2. Abhängigkeiten installieren
pip install -r requirements.txt

# 3. .env aus Vorlage kopieren und anpassen
cp .env.example .env
# → mindestens ADMIN_PASSWORD und SECRET_KEY ändern.

# 4. DB initialisieren (inkl. Beispiel-Festivaltagen)
python init_db.py --with-days

# 5. Server starten
uvicorn app.main:app --reload --port 8000
```

- Anmeldeformular: <http://localhost:8000/>
- Helfer-Login:     <http://localhost:8000/login>
- Admin:            <http://localhost:8000/admin>

Alternativ `./run.sh`, das Schritte 1–5 in einem macht.

Für lokale UI-Tests des Passwort-vergessen-Flows ohne SMTP: in der `.env`
`DEBUG_SHOW_RESET_LINK=true` setzen — dann erscheint der Reset-Link direkt
auf der Seite. **In Produktion auf `false` lassen.**

### `init_db.py`-Flags

| Flag | Wirkung |
|---|---|
| *(keins)* | Tabellen anlegen/aktualisieren, Spalten-Migrationen, Default-Bereiche+Rollen seeden |
| `--with-days` | zusätzlich 4 Beispiel-Festivaltage (Do–So in ~3 Monaten) |
| `--reset` | ⚠️ **Alle Tabellen löschen** und neu anlegen |

Das Script ist idempotent: wiederholtes Aufrufen ist sicher. Neue Spalten
(`password_hash`, `pfand_paid`, …) werden per `ALTER TABLE ADD COLUMN`
nachgezogen, inkl. Default-Backfill für Booleans.

## Projektstruktur

```
chimaera-helfer-tool/
├── app/
│   ├── main.py               FastAPI-Einstieg, mountet alle Router
│   ├── config.py             Settings aus .env (pydantic-settings)
│   ├── database.py           SQLAlchemy Engine + Session
│   ├── models.py             Alle Tabellen (Helper, Shift, SwapOffer, …)
│   ├── auth.py               Admin- UND Helfer-Session (signierte Cookies)
│   ├── passwords.py          PBKDF2-Hashing (stdlib, keine externen Deps)
│   ├── csv_io.py             CSV-Import/-Export für Helfer-Liste
│   ├── email_sender.py       SMTP-Versand (Reset, Swap, Mail-Verteiler)
│   └── routers/
│       ├── public.py         /, /register, /login, /logout, /forgot, /reset
│       ├── helper_area.py    /me, /board, Swap-Flows
│       └── admin_pages.py    /admin/*
├── app/templates/            Jinja2
├── app/static/               Tailwind CDN + etwas CSS
├── init_db.py                Tabellen + Seed + Migrationen
├── smoke_test.py             End-to-End-HTTP-Tests (eigener uvicorn)
├── requirements.txt
├── .env.example
├── Dockerfile                Python-slim, für Render/Fly/Railway
├── render.yaml               Render.com Blueprint
└── run.sh                    Lokaler Convenience-Starter
```

## Admin-Zugang

**Credentials kommen aus `.env`** (`ADMIN_USERNAME` + `ADMIN_PASSWORD`).
Es gibt absichtlich kein Admin-User-Management in der Datenbank — für eine
einzelne verantwortliche Person völlig ausreichend und ein Angriffsvektor
weniger.

**⚠️ Vor dem ersten öffentlichen Deploy unbedingt:**

1. `ADMIN_PASSWORD` auf ein starkes, eigenes Passwort setzen.
2. `SECRET_KEY` auf einen langen Zufallsstring setzen:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   Der Key signiert die Session-Cookies. Wenn er sich ändert, werden alle
   eingeloggten Sessions ungültig — also nicht unbewusst wechseln.
3. `DEBUG_SHOW_RESET_LINK=false` setzen (oder weglassen — Default ist
   `false`).

Logout liegt unter `POST /admin/logout`.

## Stammdaten pflegen

Unter `/admin/config`:

- **Festivaltage:** Datum + Label (z.B. „Donnerstag (Aufbau)"). Löschen
  kaskadiert auf alle Verfügbarkeiten und Schichten dieses Tages.
- **Einsatzbereiche:** Name + optionale Beschreibung. Die Beschreibung
  taucht im Anmeldeformular auf. Default werden beim ersten `init_db.py`-Lauf
  Bar / Einlass / Aufbau / Abbau / Infopoint / Crew Catering angelegt, je
  mit sinnvollen Start-Rollen.
- **Rollen:** pro Bereich beliebig viele.

**Empfehlung:** Stammdaten vor dem Öffnen des Anmeldeformulars fixieren.
Nachträgliche Tage hinzufügen geht, ist aber fair nur, wenn noch keine
Helfer:innen angemeldet waren — sonst müsstest du sie anschreiben.

## Schichtplanung + Rollen-Zutrauen

**Typischer Workflow, z.B. für die Bar:**

1. **Stammdaten:** Rollen anlegen (Barleitung, Springer, Tresenkraft,
   Runner).
2. **Nach Anmeldeschluss:** je Helfer:in pro Bereich ankreuzen, welche
   Rollen du ihr zutraust. Auf `/admin/helpers/<id>` im Panel
   „Admin-Notizen". Handarbeit, aber wichtig — die Schichtplanung sieht
   sonst jede:n gleich.
3. **Schichten anlegen:** `/admin/shifts` → „+ Neue Schicht". Bereich, Tag,
   Start-/Endzeit, Kapazität (gleichzeitige Plätze).
4. **Zuweisen:** Schicht öffnen → rechts die Kandidat:innen-Liste.
   Sortiert nach Wunsch-Rang (1. Wahl zuerst), dann Vorname. Bei jeder
   Person siehst du zugetraute Rollen. Beim Zuweisen wählst du optional
   eine konkrete Rolle; nicht-zugetraute Rollen sind im Dropdown explizit
   als „nicht zugetraut" markiert (erlaubt sie aber trotzdem, falls du
   weißt was du tust).

**Schichttausch** (läuft ohne Admin, sofern konfliktfrei):

- **Board:** Helfer:in stellt ihre Schicht auf `/board`. Wer sie übernimmt,
  klickt „Übernehmen". Das System prüft zeitliche Überschneidungen und
  fügt den Tag ggf. stillschweigend zur Verfügbarkeit der übernehmenden
  Person hinzu.
- **Direkter Tausch:** Email einer Freund:in eingeben, optional Nachricht.
  Die Freund:in bekommt die Anfrage auf `/me` (und per Mail, falls SMTP) und
  akzeptiert oder lehnt ab. Beim Akzeptieren werden alle anderen offenen
  Anfragen/Angebote auf diese Schicht automatisch abgebrochen.

**Wichtig:** Beim Tausch bleibt die zugewiesene **Rolle** erhalten. Wenn
die neue Person keine Trust-Markierung für diese Rolle hat, wird das nicht
blockiert — der Admin kann im Nachgang prüfen und ggf. umplanen.

## Pfand-Tracking

Im Panel „Pfand" auf `/admin/helpers/<id>`:

- **„Pfand bezahlt"** ankreuzen → Timestamp wird automatisch gesetzt.
- **„Pfand zurückgegeben"** ankreuzen → weiterer Timestamp.
- „Pfand bezahlt" wieder entfernen → auch „zurückgegeben" geht aus, beide
  Timestamps werden zurückgesetzt. Konsistenzgedanke: ein nicht bezahltes
  Pfand kann nicht zurückgegeben sein.

In der Helfer-Liste erscheint eine Pfand-Spalte (— / bezahlt / ✓ zurück)
und ein Pfand-Filter. Das Dashboard zeigt zwei Kennzahlen:
- „bezahlt, noch nicht zurück" (Was du nach dem Festival noch auszahlen
  musst)
- „vollständig abgewickelt"

## CSV-Import / -Export

**Spalten im Export** (Semikolon-getrennt, UTF-8, mit Header):

```
id;first_name;last_name;email;phone;date_of_birth;iban;paypal;
been_here_before;previous_festivals;availability_days;preferred_areas;
notes;admin_notes;status;pfand_paid;pfand_paid_at;
pfand_returned;pfand_returned_at;created_at
```

- `date_of_birth`: ISO-Datum (`YYYY-MM-DD`)
- `been_here_before`, `pfand_paid`, `pfand_returned`: `ja` / `nein`
- `pfand_paid_at`, `pfand_returned_at`: ISO-Timestamp oder leer
- `availability_days`: Tage getrennt mit `|`, Labels wie in Stammdaten
  (z.B. `Donnerstag (Aufbau)|Freitag`)
- `preferred_areas`: mit Rang, Pipe-getrennt (z.B. `1:Bar|2:Einlass`)
- `notes`, `admin_notes`: Newlines werden zu Leerzeichen

**Import** erwartet dieselben Spalten. Minimum ist `email`, `first_name`,
`last_name`, `date_of_birth`. Bestehende Einträge werden per `email`
abgeglichen und aktualisiert, sonst neu angelegt. Alte CSVs ohne die
Pfand-Spalten werden weiter akzeptiert — die Felder bleiben einfach leer.

**Aus dem Import angelegte Helfer:innen haben kein Passwort.** Sie können
sich via „Passwort vergessen" eins setzen, oder der Admin erzeugt ihnen auf
`/admin/helpers/<id>` einen Reset-Link.

## SMTP (optional) konfigurieren

Wenn `SMTP_HOST`, `SMTP_USER` und `SMTP_PASSWORD` in `.env` gesetzt sind,
kann das Tool:

- den Mail-Verteiler direkt versenden (mit Platzhalter-Ersetzung)
- Passwort-Reset-Links automatisch mailen
- Tausch-Anfragen + -Bestätigungen als Benachrichtigungs-Mails senden

Ohne SMTP:
- Mail-Verteiler bietet nur „Empfängerliste kopieren / CSV"
- Reset-Links werden in die Server-Konsole gedruckt; Admin kann sie
  zusätzlich manuell über den Detail-Button erzeugen
- Swap-Benachrichtigungen passieren nur in-app

**Gmail / Google Workspace als Absender:** App-Passwort nötig (nicht das
Account-Passwort), `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
`SMTP_USE_TLS=true`.

Für kleine Vereine ist ein transaktionaler Dienst wie Mailgun, Postmark,
Brevo oder auch der hauseigene Mailserver empfehlenswert — alle haben
Free Tiers, die ein paar hundert Mails/Monat problemlos abdecken.

## Smoke-Tests

`smoke_test.py` startet einen eigenen uvicorn auf Port 8766 und klappert 75
Checks durch (öffentliche Anmeldung inkl. Validierung, Admin-Flows inkl.
aller Edit-Sections und Pfand-Logik, Filter-Regressionen, Helfer-Auth mit
Reset, Tausch-Board, direkter Tausch). Separate DB `chimaera_smoke.db` —
deine Produktions-DB wird nicht beeinflusst.

```bash
. .venv/bin/activate
pip install httpx  # nur für den Test
python smoke_test.py
```

## Deployment

Siehe `DEPLOYMENT.md` für Schritt-für-Schritt-Anleitung zu Render.com +
Neon Postgres (beide kostenlos für diesen Anwendungsfall) sowie
Alternativen (Railway, Fly.io) und eine Produktions-Checkliste.

## Lizenz + Kontext

Entwickelt für Chimaera e.V. (Dresden). Interner Gebrauch; keine Garantie.
