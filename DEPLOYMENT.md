# Deployment: Render.com + Neon Postgres

Dieser Guide deployt das Helfer-Tool auf Render.com (Web-Service) + Neon
(Postgres). Beides genügt für 100–200 Nutzer:innen einmal im Jahr locker,
auch auf den kostenlosen Tiers. Alternativen (Railway, Fly.io) am Ende.

**Zeitbedarf beim ersten Mal:** ca. 30–45 Minuten, Hälfte Warten.

**Free-Tier-Einschränkungen:**
- Render Free-Tier schläft nach 15 Min Inaktivität ein. Erster Request
  nach dem Schlaf: 30–60s. Für ein Tool, das nur ein paar Wochen pro Jahr
  intensiv genutzt wird, gut genug. Wenn's nerven sollte, 7 USD/Monat auf
  den Starter-Plan.
- Neon Free-Tier schläft auch ein, wacht aber in ~2 s auf.
- Beide decken 100–200 Helfer:innen problemlos ab.

---

## 0. Produktions-Checkliste vorab

Bevor du den ersten Deploy machst, sicherstellen:

- [ ] In der Codebasis-`.env` **keine** echten Credentials (die `.env` gehört
  nicht ins Git-Repo; `.gitignore` schließt sie aus).
- [ ] `ADMIN_PASSWORD` ist ein eigenes, starkes Passwort (nicht das Default).
- [ ] `SECRET_KEY` ist ein langer Zufallsstring, z.B.
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
  (Render generiert automatisch einen — für eigene Hostings selbst setzen.)
- [ ] `DEBUG_SHOW_RESET_LINK=false` (Default, nicht überschreiben).
- [ ] `DATABASE_URL` zeigt auf eine echte Postgres-DB, nicht SQLite.
- [ ] SMTP-Zugangsdaten für den Absender-Account eingerichtet und getestet.
- [ ] Server-Logs im Blick behaltbar (Render-Dashboard → Logs-Tab).
- [ ] `.env`-Backup an sicherem Ort, nicht im Klartext im Team-Chat.

---

## 1. Neon Postgres einrichten

1. Auf <https://neon.tech/> registrieren (GitHub-Login geht).
2. **New Project** → Name z.B. `chimaera-helfer`, Region
   **eu-central-1 (Frankfurt)** (DSGVO + Latenz).
3. Dashboard zeigt den **Connection String**:
   ```
   postgresql://username:password@ep-xxx-yyy.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```
   Kopieren — das wird `DATABASE_URL`.
4. (Optional) Unter **Roles** eigenen Nutzer anlegen, wenn du den Default
   nicht verwenden willst.

Keine Tabellen anlegen — das macht `init_db.py` beim ersten App-Start.

---

## 2. GitHub-Repo

```bash
cd chimaera-helfer-tool
git init
git add .
git commit -m "Initial commit"

# Auf github.com ein neues PRIVATES Repo anlegen, dann:
git remote add origin git@github.com:<user>/chimaera-helfer-tool.git
git branch -M main
git push -u origin main
```

**Sicherheit:** `.env` und `*.db` dürfen nicht ins Repo. Die `.gitignore`
schließt beides aus — vor dem ersten Push `git status` prüfen, nichts
Sensitives in der Staging-Area.

---

## 3. Render.com einrichten

1. Account auf <https://render.com/> (GitHub-Login vereinfacht Schritt 2).
2. **Authorize GitHub** → Render braucht Zugriff auf dein Repo. Zugriff kann
   auf einzelne Repos beschränkt werden.
3. **New +** → **Blueprint** → Repo wählen.
4. Render liest `render.yaml` und schlägt den Service vor:
   `chimaera-helfer-tool`, Typ `web`, Runtime `docker`, Plan `free`.
5. Bestätigen. Jetzt fragt Render die Env-Vars ab, die im Blueprint als
   `sync: false` (manuell zu setzen) markiert sind.

### Env-Vars setzen

| Variable | Wert |
|---|---|
| `DATABASE_URL` | Connection String aus Neon (Schritt 1) |
| `ADMIN_PASSWORD` | dein starkes Admin-Passwort |
| `SMTP_HOST` | z.B. `smtp.eure-domain.de` |
| `SMTP_USER` | Account-Name |
| `SMTP_PASSWORD` | App-Passwort / SMTP-Passwort |
| `SMTP_FROM_ADDRESS` | z.B. `helfer@chimaera-festival.de` |

`SECRET_KEY` wird von Render automatisch generiert (`generateValue: true`
im Blueprint), `ADMIN_USERNAME` bleibt auf `admin` (oder eigener Wert).
`FESTIVAL_NAME` und die `MIN_*`-Defaults sind im Blueprint verdrahtet und
können dort direkt angepasst werden.

### Erster Deploy

Nach „Apply" startet der Build. Dockerfile wird gebaut, Container startet
mit:
```bash
python init_db.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`init_db.py` ist idempotent — legt Tabellen an, seedet Default-Bereiche,
zieht neue Spalten nach. **Nichts manuell triggern.**

Render gibt dir eine URL wie `https://chimaera-helfer-tool.onrender.com`.
Test:
- `/` → Anmeldeformular (zeigt „Noch keine Festivaltage konfiguriert" —
  richtig, noch keine angelegt).
- `/admin/login` → Admin-Login mit `admin` + deinem Passwort.

### SMTP nach dem Deploy testen

1. Als Admin einloggen, `/admin/mail` öffnen.
2. Einen Testhelfer anmelden (oder dich selbst mit einer anderen Email).
3. Im Mail-Verteiler: Betreff + kurzer Text, Checkbox **„Nur Testmail"**
   aktivieren, senden.
4. Kommt die Mail? Gut. Wenn nicht: Render-Logs checken (die Fehlermeldung
   vom SMTP-Versuch steht dort).

### Festivaltage anlegen

Unter `/admin/config`:
1. Festivaltage eintragen.
2. Bereiche prüfen (sind durch Seed schon da).
3. Rollen kontrollieren (Defaults da, bei Bedarf ergänzen).

Jetzt ist das Tool bereit für öffentliche Anmeldungen.

---

## 4. Custom Domain (optional)

1. Render: Service → **Settings** → **Custom Domains** → **Add**.
2. Render zeigt den CNAME-Target (z.B. `xxx.onrender.com`).
3. Beim Domain-Provider einen **CNAME-Record** anlegen, z.B.
   `helfer.chimaera-festival.de` → Render-Target.
4. Nach 5–15 Min (DNS-Propagation) in Render „Verify" — Let's-Encrypt-Zert
   wird automatisch ausgestellt.

Apex-Domain ohne Subdomain: entweder ALIAS/ANAME beim Provider, oder
A-Records auf Renders feste IPs (stehen im Dashboard).

---

## 5. Alternativen

### Railway.app

```
New Project → Deploy from GitHub Repo (Dockerfile wird erkannt).
+ New → Database → PostgreSQL (DATABASE_URL wird automatisch als Env-Var
gesetzt).
Variables-Tab: ADMIN_PASSWORD, SECRET_KEY, SMTP_*.
Settings → Networking → Generate Domain.
```

Railway schläft nicht ein (bei aktivem Guthaben), ist also immer warm.
5 USD Gratis-Guthaben/Monat reichen für diesen Anwendungsfall.

### Fly.io

CLI-basiert. Free Tier 3 kleine VMs, keine Schlafzeit.

```bash
fly auth signup
fly launch   # Ja zu Dockerfile, DB nach Wahl (Neon extern oder Fly-Postgres)
fly secrets set \
  DATABASE_URL="postgresql://..." \
  ADMIN_PASSWORD="..." \
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  SMTP_HOST="..." SMTP_USER="..." SMTP_PASSWORD="..." SMTP_FROM_ADDRESS="..."
fly deploy
```

---

## 6. Rund ums Deployment

### Backups

Neon legt automatische Snapshots an (Retention je nach Tier 24h–30 Tage).
Für wichtige Checkpoints (z.B. vor der Festival-Woche) zusätzlich von Hand:

```bash
pg_dump "$DATABASE_URL" > chimaera-backup-$(date +%Y%m%d).sql
```

Vor wichtigen Deploys idealerweise auch einmal.

### Updates ausrollen

Jeder Push auf `main` triggert bei Render (mit `autoDeploy: true`) einen
neuen Deploy. Dauer: 2–3 Min. `init_db.py` läuft bei jedem Start und zieht
fehlende Spalten nach — einfache Schema-Änderungen gehen ohne manuelle
Migration durch. Komplexere Änderungen (Spalte umbenennen, Constraint
ändern, Datenmigration) lieber vorher lokal testen und ein SQL-Script
vorbereiten.

### Admin-Passwort ändern

`ADMIN_PASSWORD` ist eine Env-Var. In Render/Railway/Fly setzen und Service
neu deployen — kein DB-Eingriff nötig.

### Helfer-Passwörter zurücksetzen (ohne SMTP oder wenn Mail nicht ankommt)

Admin öffnet `/admin/helpers/<id>`, klickt „Passwort-Reset-Link erzeugen".
Der Link wird einmalig angezeigt, ist 24h gültig. Per beliebigem Kanal
weitergeben (Signal, SMS, Telegram).

### Logs ansehen

- Render: Dashboard → Service → **Logs**. Enthält auch Print-Ausgaben aus
  `email_sender.py` (SMTP-Fehler) und vom Passwort-Vergessen-Flow
  (Reset-Links bei deaktiviertem SMTP).
- Railway / Fly: ähnliche Live-Log-Viewer.

### `SECRET_KEY` nicht unbewusst wechseln

Bei Wechsel werden alle Sessions ungültig. Admin-Login ist neu nötig, aber
nicht kritisch. Bei Helfer:innen ist das nur Komfort. Wechsel daher nur
bewusst vornehmen, nie versehentlich (z.B. durch einen Service-Neustart ohne
gesetzte Env-Var — dann könnte ein Auto-Generator neu würfeln).

### Festival ist vorbei, nächstes Jahr soll wieder starten

Einfach die Daten in der DB liegenlassen. Für die nächste Ausgabe:

1. Stammdaten aktualisieren: alte Festivaltage löschen, neue anlegen.
2. Bereiche/Rollen ggf. anpassen.
3. Optional: alte Helfer:innen per CSV-Export archivieren und per
   `DELETE FROM helpers WHERE created_at < '2026-01-01'` rausräumen.
   Cascade räumt Zuweisungen, Swap-Anfragen etc. automatisch mit weg.

Oder die DB leeren und frisch starten mit `python init_db.py --reset` —
macht aber jede Historie platt.
