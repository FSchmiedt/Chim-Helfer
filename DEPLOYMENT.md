# Deployment: Render.com + Neon Postgres

Render.com (Web-Service) + Neon (Postgres). Beides genügt für 100–200
Nutzer:innen einmal im Jahr locker, auch auf den kostenlosen Tiers.
Alternativen (Railway, Fly.io) am Ende.

**Zeitbedarf beim ersten Mal:** ca. 30–45 Minuten, Hälfte Warten.

---

## 0. Produktions-Checkliste vorab

- [ ] In der lokalen `.env` **keine** echten Credentials (`.env` ist via
  `.gitignore` ausgeschlossen).
- [ ] `ADMIN_PASSWORD` ist ein eigenes, starkes Passwort.
- [ ] `SECRET_KEY` ist ein langer Zufallsstring:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
  (Render generiert automatisch einen — selbst hosten: selbst setzen.)
- [ ] `DEBUG_SHOW_RESET_LINK=false` (Default).
- [ ] `DATABASE_URL` zeigt auf eine echte Postgres-DB, nicht SQLite.
- [ ] SMTP-Zugangsdaten eingerichtet und getestet (Gmail/Workspace:
  App-Passwort generieren).
- [ ] `.env`-Backup an sicherem Ort.

---

## 1. Neon Postgres einrichten

1. <https://neon.tech/> registrieren (GitHub-Login geht).
2. **New Project** → Name z.B. `chimaera-helfer`, Region
   **eu-central-1 (Frankfurt)** (DSGVO + Latenz).
3. Connection String kopieren — wird `DATABASE_URL`.
4. Tabellen werden vom App-Start (`init_db.py`) angelegt.

---

## 2. GitHub-Repo

```bash
cd chimaera-helfer-tool
git init
git add .
git commit -m "Initial commit"
git remote add origin git@github.com:<user>/<repo>.git
git branch -M main
git push -u origin main
```

**Sicherheit:** `.env` und `*.db` dürfen nicht ins Repo. `.gitignore`
schließt beides aus — vor dem ersten Push `git status` prüfen.

---

## 3. Render.com einrichten

1. Account auf <https://render.com/>.
2. **Authorize GitHub** auf das gewünschte Repo.
3. **New +** → **Blueprint** → Repo wählen.
4. Render liest `render.yaml`, schlägt Service `chimaera-helfer-tool`
   vor (Region Frankfurt, Plan free).
5. Env-Vars befüllen (siehe unten). „Apply".

### Env-Vars

| Variable | Wert |
|---|---|
| `DATABASE_URL` | Connection String aus Neon |
| `ADMIN_PASSWORD` | starkes Admin-Passwort |
| `SMTP_HOST` | `smtp.gmail.com` (für Google Workspace) |
| `SMTP_USER` | Absender-Email-Adresse |
| `SMTP_PASSWORD` | App-Passwort des Absender-Accounts |
| `SMTP_FROM_ADDRESS` | dieselbe Email-Adresse |

`SECRET_KEY` wird automatisch generiert. `ADMIN_USERNAME`, `FESTIVAL_NAME`
und SMTP-Defaults sind im Blueprint verdrahtet.

### Erster Deploy

Render baut Dockerfile, startet Container mit:
```bash
python init_db.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`init_db.py` ist idempotent — legt Tabellen an, seedet Default-Bereiche,
zieht neue Spalten nach. **Nichts manuell triggern.**

URL aufrufen, mit `admin` + Passwort einloggen.

### SMTP-Test

1. `/admin/mail` öffnen.
2. Test-Helfer anmelden (mit echter Email).
3. Im Verteiler: Betreff + Text, „Nur Testmail" anhaken, senden.
4. Mail kommt an? Gut. Falls nicht: Render-Logs prüfen.

### Festivaltage anlegen

`/admin/config`:
1. Festivaltage eintragen.
2. Bereiche prüfen.
3. Rollen kontrollieren.

---

## 4. Bereiche/Tage neu seeden in Produktion

Wenn du in einer früheren Version andere Default-Bereiche oder andere
Festivaltage hattest und die Render-DB jetzt mit den aktuellen Defaults
synchen willst, musst du **einmalig** ein Migrations-Kommando in der
Render-Shell laufen lassen.

**⚠️ Wichtig:** `--reseed-areas` löscht alle bestehenden Bereiche
(kaskadiert auf Helfer-Wunschbereiche, Rollen-Zutrauen, Schichten).
`--reseed-days` löscht alle Festivaltage (kaskadiert auf Verfügbarkeiten,
Schichten). Wenn schon Helfer:innen angemeldet sind, geht deren Auswahl
verloren — das ist nur OK in der Setup-Phase, **bevor** das Anmeldeformular
veröffentlicht wurde.

### Über Render Shell

1. Render Dashboard → dein Service → Tab **Shell** (oben in der
   Service-Navigation).
2. Im geöffneten Terminal:
   ```bash
   python init_db.py --reseed-areas --reseed-days
   ```
3. Output sollte zeigen:
   ```
   ✓ Bereiche+Rollen RE-SEEDED
   ✓ Festivaltage RE-SEEDED auf Fr/Sa/So
   ℹ️  8 Bereiche und 3 Festivaltage in der DB
   ```

Falls die Shell nicht verfügbar ist (manchmal Render Free-Tier),
Alternative: lokal mit der Production-`DATABASE_URL` arbeiten:
```bash
DATABASE_URL="postgresql://...neon.tech/neondb?sslmode=require" \
    python init_db.py --reseed-areas --reseed-days
```

### Über manuelle Bedienung im Admin-UI

Wenn du nicht alles in einem Rutsch reseeden willst, kannst du auch unter
`/admin/config` einzelne Tage und Bereiche von Hand löschen und die
gewünschten neu anlegen. Etwas mühsamer, aber feinkörniger.

### Ist `--reseed-areas` sicher, wenn schon Helfer:innen angemeldet sind?

Nein. Der Befehl löscht **alle** Bereiche per `DELETE FROM areas`. Cascade
entfernt damit:
- alle Wunschbereich-Einträge der Helfer:innen
- alle Rollen-Zutrauen
- alle Schichten dieser Bereiche

Die Helfer:innen-Personen selbst bleiben, mit ihrem Login, ihrem Pfand-
Status, ihrer Verfügbarkeit. Die Wunschbereiche müssten sie aber neu
auswählen — das wäre frustrierend. Vor öffentlichem Launch reseeden, danach
nur einzelne Bereiche im UI ergänzen.

---

## 5. Custom Domain (optional)

1. Render: Service → **Settings** → **Custom Domains** → **Add**.
2. Render zeigt CNAME-Target.
3. Beim Domain-Provider CNAME anlegen (`helfer.chimaera-festival.de` →
   Render-Target).
4. Nach DNS-Propagation in Render „Verify". Let's-Encrypt wird automatisch.

---

## 6. Alternativen

### Railway.app
```
New Project → Deploy from GitHub Repo (Dockerfile wird erkannt).
+ New → Database → PostgreSQL.
Variables: ADMIN_PASSWORD, SECRET_KEY, SMTP_*.
Settings → Networking → Generate Domain.
```
Kein Schlafmodus bei aktivem Guthaben. 5 USD Gratis/Monat.

### Fly.io
CLI-basiert, keine Schlafzeit.
```bash
fly auth signup
fly launch
fly secrets set DATABASE_URL=... ADMIN_PASSWORD=... SECRET_KEY=...
fly deploy
```

---

## 7. Rund ums Deployment

### Backups

Neon hat automatische Snapshots (Free 24h Retention). Vor wichtigen Daten
zusätzlich:
```bash
pg_dump "$DATABASE_URL" > chimaera-backup-$(date +%Y%m%d).sql
```

### Updates ausrollen

Push auf `main` triggert Auto-Deploy (2–3 Min). `init_db.py` zieht neue
Spalten nach. Bei größeren Schema-Änderungen vorher lokal testen.

### Admin-Passwort ändern

Render → Environment → `ADMIN_PASSWORD` ändern → Save → Auto-Redeploy.

### Helfer-Passwörter ohne SMTP zurücksetzen

`/admin/helpers/<id>` → „Passwort-Reset-Link erzeugen". Link ist 24h gültig.

### Email-Verifikations-Link manuell zustellen

Wenn die Verifikations-Mail nicht ankommt: in den Render-Logs steht der
Verifikations-Link mit Prefix `[register] Verifikations-Link für ...`
(falls SMTP gerade kaputt ist) oder die Person fordert über `/me` einen
neuen Link an. Falls die Mail trotz funktionierendem SMTP nicht ankommt,
hat sie wahrscheinlich im Spam-Ordner gelandet — die Person bitten, dort
zu prüfen.

### Logs ansehen

Render Dashboard → Service → **Logs**. Live-Stream. Print-Ausgaben aus
`email_sender.py` und vom Verifikations-/Reset-Flow erscheinen dort.

### `SECRET_KEY` nicht unbewusst wechseln

Bei Wechsel werden alle Sessions ungültig. Daher nur bewusst tauschen.

### Festival vorbei, nächstes Jahr neu starten

1. CSV-Export der aktuellen Helfer:innen ziehen (Archiv).
2. Stammdaten aktualisieren (`/admin/config`): alte Festivaltage durch neue
   ersetzen.
3. Bereiche/Rollen ggf. anpassen.
4. Optional: alte Daten per Render-Shell löschen
   ```sql
   DELETE FROM helpers WHERE created_at < '2026-01-01';
   ```
   Cascade räumt Zuweisungen, Swap-Anfragen etc. mit weg.

Oder ganz frisch mit `python init_db.py --reset` — macht die komplette
Historie platt.
