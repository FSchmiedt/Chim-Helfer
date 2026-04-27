# Chimaera Helfer-Tool

Ein kleines FastAPI-Tool, um Helfer:innen-Anmeldungen, Schichtplanung und
Schicht-Tausch für ein einmal-im-Jahr-Festival zu verwalten. Gebaut mit
FastAPI + SQLAlchemy + Jinja2-Templates + Tailwind. SQLite lokal, Postgres
in Produktion.

## Was das Tool macht

**Für Helfer:innen (öffentlich unter `/`):**
- Anmeldung mit Name, Kontakt, Geburtsdatum (Volljährigkeits-Check),
  Tag-Verfügbarkeit, Wunschbereichen mit Ranking (1. Wahl … 5 = egal/Default),
  IBAN **oder** PayPal für Pfand-Auszahlung (Pflicht — eines von beidem),
  Passwort.
- **Email-Verifikation** nach Anmeldung: Mail mit Bestätigungs-Link wird
  rausgeschickt. Login funktioniert auch ohne Verifikation, aber im
  `/me`-Bereich erscheint ein Banner und Admin sieht den Status.
- Eigener Login-Bereich `/me`: Schichten einsehen, Passwort ändern,
  Verifikations-Mail erneut anfordern.
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
  **Pfand-Status, Email-Verifikation**, Volltext über Name/Email.
- **Helfer-Detail:** alle Felder editierbar in Panels, die unabhängig
  gespeichert werden (Stammdaten / Verfügbarkeit+Wünsche / Admin-Bereich /
  Pfand). Kein versehentliches Überschreiben.
- **Rollen-Zutrauen** pro Helfer:in pro Bereich — Schichtplanung zeigt das
  als Hinweis (z.B. Bar → Barleitung, Springer, Tresenkraft, Runner).
- **Schichten:** anlegen (Bereich, Tag, Zeit, Kapazität), Kandidat:innen
  zuweisen (sortiert nach Wunsch-Rang, mit Hinweis auf zugetraute Rollen).
- **Pfand-Tracking:** „Pfand bezahlt" und „Pfand zurückgegeben" pro Helfer,
  mit automatischen Timestamps. Beim Wegnehmen von „bezahlt" wird
  „zurückgegeben" automatisch mit entfernt. Filter + Dashboard-Kennzahlen.
- **Stammdaten:** Festivaltage, Einsatzbereiche, Rollen.
- **Mail-Verteiler:** filtern, Empfängerliste kopieren (BCC) oder via SMTP
  versenden. Platzhalter: `{{Vorname}}`, `{{Nachname}}`, `{{FestivalName}}`.
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

## Validierung der Anmeldung

Das Anmeldeformular validiert serverseitig (in `app/routers/public.py`):

- **Vorname/Nachname:** Pflichtfeld, leerer String wird abgelehnt
- **Email:** Pflichtfeld, RFC-konform, eindeutig (Duplikat erzeugt Hinweis)
- **Geburtsdatum:** Pflichtfeld, mindestens 18 Jahre (rechnet das Alter
  selbst aus — der Volljährigkeits-Check passiert nicht nur über die
  Checkbox, die ist eine zusätzliche Bestätigung)
- **Verfügbarkeit:** mindestens ein Tag muss angekreuzt sein
- **Wunschbereiche:** alle Bereiche aus der DB werden gespeichert; Felder,
  die leer gelassen werden, bekommen automatisch Prio 5 (= "egal/Default")
- **IBAN oder PayPal:** mindestens eines muss angegeben sein. IBAN wird mit
  ISO-13616 mod-97-Prüfsumme validiert, PayPal akzeptiert Email-Adresse,
  `@handle`-Format oder `paypal.me/<name>`-Link
- **Passwort:** mindestens 8 Zeichen, muss zweimal identisch eingegeben
  werden

Fehlermeldungen sind auf Deutsch und nennen das betroffene Feld
(`_humanize_errors` in `public.py`).

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

# 4. DB initialisieren (inkl. Beispiel-Festivaltagen Fr/Sa/So)
python init_db.py --with-days

# 5. Server starten
uvicorn app.main:app --reload --port 8000
```

- Anmeldeformular: <http://localhost:8000/>
- Helfer-Login:     <http://localhost:8000/login>
- Admin:            <http://localhost:8000/admin>

Alternativ `./run.sh`, das Schritte 1–5 in einem macht.

Für lokale UI-Tests des Passwort-vergessen- und des Email-Verifikations-Flows
ohne SMTP: in der `.env` `DEBUG_SHOW_RESET_LINK=true` setzen — dann erscheint
der Link direkt auf der Seite. **In Produktion auf `false` lassen.**

### `init_db.py`-Flags

| Flag | Wirkung |
|---|---|
| *(keins)* | Tabellen anlegen/aktualisieren, Spalten-Migrationen, Default-Bereiche+Rollen seeden (überspringt, falls schon welche da sind) |
| `--with-days` | zusätzlich 3 Beispiel-Festivaltage (Fr/Sa/So in ~3 Monaten) |
| `--reseed-areas` | ⚠️ **Bereiche+Rollen löschen** und die aktuellen Defaults neu anlegen (cascade entfernt damit Helfer-Wunschbereiche+Rollen-Zutrauen+Schichten der alten Bereiche) |
| `--reseed-days` | ⚠️ **Festivaltage löschen** und Fr/Sa/So neu anlegen (cascade entfernt Verfügbarkeiten+Schichten) |
| `--reset` | ⚠️⚠️ **Alle Tabellen löschen** und neu anlegen — nuklear |

Das Script ist ansonsten idempotent: wiederholtes Aufrufen ist sicher. Neue
Spalten werden per `ALTER TABLE ADD COLUMN` nachgezogen, inkl. Backfill.

**Migration auf einer Render-Instanz** (wenn dort schon alte Bereiche/Tage
in der Neon-DB stehen): siehe `DEPLOYMENT.md` Abschnitt „Bereiche/Tage neu
seeden in Produktion".

## Projektstruktur

```
chimaera-helfer-tool/
├── app/
│   ├── main.py               FastAPI-Einstieg, mountet alle Router
│   ├── config.py             Settings aus .env (pydantic-settings)
│   ├── database.py           SQLAlchemy Engine + Session
│   ├── models.py             Alle Tabellen
│   ├── auth.py               Admin- UND Helfer-Session (signierte Cookies)
│   ├── passwords.py          PBKDF2-Hashing (stdlib)
│   ├── csv_io.py             CSV-Import/-Export
│   ├── email_sender.py       SMTP-Versand (Reset, Verify, Swap, Verteiler)
│   └── routers/
│       ├── public.py         /, /register, /login, /logout, /forgot,
│       │                     /reset, /verify/{token}
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
einzelne verantwortliche Person ausreichend und ein Angriffsvektor weniger.

**⚠️ Vor dem ersten öffentlichen Deploy unbedingt:**

1. `ADMIN_PASSWORD` auf ein starkes, eigenes Passwort setzen.
2. `SECRET_KEY` auf einen langen Zufallsstring setzen:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   Der Key signiert die Session-Cookies. Bei Wechsel werden alle Sessions
   ungültig — also nicht unbewusst tauschen.
3. `DEBUG_SHOW_RESET_LINK=false` (Default).

## Stammdaten pflegen

Unter `/admin/config`:

- **Festivaltage:** Datum + Label. Löschen kaskadiert auf alle
  Verfügbarkeiten und Schichten dieses Tages.
- **Einsatzbereiche:** Name + optionale Beschreibung (wird derzeit nicht im
  öffentlichen Anmeldeformular angezeigt). Default beim ersten
  `init_db.py`-Lauf: Verkehr / Einlass / Cleaning / Bar / Crew Catering /
  Driver / Abbau / Awareness.
- **Rollen:** pro Bereich beliebig viele.

**Empfehlung:** Stammdaten vor dem Öffnen des Anmeldeformulars fixieren.

## Schichtplanung + Rollen-Zutrauen

**Workflow z.B. für die Bar:**

1. Stammdaten: Rollen anlegen (Barleitung, Springer, Tresenkraft, Runner).
2. Nach Anmeldeschluss: pro Helfer:in pro Bereich ankreuzen, welche Rollen
   du zutraust. Auf `/admin/helpers/<id>` im Panel „Admin-Notizen".
3. Schichten anlegen: `/admin/shifts` → „+ Neue Schicht".
4. Zuweisen: Schicht öffnen → Kandidat:innen sortiert nach Wunsch-Rang.
   Beim Zuweisen wählst du optional eine konkrete Rolle.

**Schichttausch** (läuft ohne Admin, sofern konfliktfrei):

- **Board:** Helfer:in stellt ihre Schicht auf `/board`. Andere übernehmen
  per Klick. System prüft Zeit-Überschneidungen.
- **Direkter Tausch:** Email einer Freund:in eingeben. Sie bekommt die
  Anfrage auf `/me` (und per Mail) und akzeptiert oder lehnt ab.

Beim Tausch bleibt die zugewiesene **Rolle** erhalten. Wenn die neue Person
die Rolle nicht zugetraut hat, blockiert das nicht — Admin kann nachschauen.

## Pfand-Tracking

Im Panel „Pfand" auf `/admin/helpers/<id>`:

- „Pfand bezahlt" ankreuzen → Timestamp gesetzt.
- „Pfand zurückgegeben" ankreuzen → weiterer Timestamp.
- „Pfand bezahlt" wieder entfernen → auch „zurückgegeben" geht aus, beide
  Timestamps werden zurückgesetzt.

Helfer-Liste hat eine Pfand-Spalte (— / bezahlt / ✓ zurück) und einen
Pfand-Filter. Dashboard zeigt zwei Kennzahlen: „bezahlt, noch nicht zurück"
und „vollständig abgewickelt".

## CSV-Import / -Export

**Spalten im Export** (Semikolon-getrennt, UTF-8, mit Header):

```
id;first_name;last_name;email;phone;date_of_birth;iban;paypal;
been_here_before;previous_festivals;availability_days;preferred_areas;
notes;admin_notes;status;pfand_paid;pfand_paid_at;
pfand_returned;pfand_returned_at;created_at
```

- `date_of_birth`: ISO (`YYYY-MM-DD`)
- `been_here_before`, `pfand_paid`, `pfand_returned`: `ja` / `nein`
- `availability_days`: Pipe-getrennt, Labels wie in Stammdaten
- `preferred_areas`: `1:Bar|2:Einlass|5:Crew Catering`
- Newlines in Notizen werden zu Leerzeichen

**Import** erwartet dieselben Spalten. Minimum: `email`, `first_name`,
`last_name`, `date_of_birth`. Bestehende Einträge werden per `email`
abgeglichen und aktualisiert. Alte CSVs ohne Pfand-Spalten werden weiter
akzeptiert.

**Aus dem Import angelegte Helfer:innen haben kein Passwort.** Sie können
sich via „Passwort vergessen" eins setzen, oder Admin erzeugt ihnen auf
`/admin/helpers/<id>` einen Reset-Link.

## SMTP konfigurieren

Wenn `SMTP_HOST`, `SMTP_USER` und `SMTP_PASSWORD` in `.env` gesetzt sind:
- Mail-Verteiler verschickt direkt
- **Email-Verifikations-Mail** geht nach Anmeldung automatisch raus
- Passwort-Reset-Links werden gemailt
- Tausch-Anfragen + Bestätigungen als Benachrichtigungs-Mails

Ohne SMTP:
- Mail-Verteiler bietet nur Copy/CSV
- Reset- und Verifikations-Links werden in die Server-Konsole gedruckt;
  Admin kann Reset-Links manuell über den Detail-Button erzeugen
- Swap-Benachrichtigungen passieren nur in-app

**Gmail / Google Workspace:** App-Passwort nötig (siehe
<https://myaccount.google.com/apppasswords>), `SMTP_HOST=smtp.gmail.com`,
`SMTP_PORT=587`, `SMTP_USE_TLS=true`. Für andere Provider sieh in deren
SMTP-Doku nach.

## Smoke-Tests

`smoke_test.py` startet einen eigenen uvicorn auf Port 8766 und klappert 76
Checks ab (öffentliche Anmeldung inkl. aller Validierungen, Admin-Flows
inkl. aller Edit-Sections und Pfand-Logik, Filter-Regressionen, Helfer-Auth
mit Reset, Tausch-Board, direkter Tausch). Separate DB
`chimaera_smoke.db` — deine Produktions-DB wird nicht beeinflusst.

```bash
. .venv/bin/activate
pip install httpx  # nur für den Test
python smoke_test.py
```

## Deployment

Siehe `DEPLOYMENT.md` für Schritt-für-Schritt-Anleitung zu Render.com +
Neon Postgres (beide kostenlos), Migrations-Hinweise für bestehende
Render-Instanzen und Alternativen (Railway, Fly.io).

## Lizenz + Kontext

Entwickelt für Chimaera e.V. (Dresden). Interner Gebrauch; keine Garantie.
