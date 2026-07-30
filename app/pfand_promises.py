"""Ueberfaellige Pfand-Zusagen: Sammelmail + automatisches Austragen.

Kein Scheduler, kein Cron. Der Durchlauf haengt an Admin-Requests (Login und
Helferuebersicht) und ist idempotent: er macht nur etwas, wenn wirklich etwas
zu tun ist, und laesst sich deshalb beliebig oft am Tag aufrufen.

Ablauf:
  Frist + 1 Tag   Sammelmail an HELFEN_EMAIL mit allen neu ueberfaelligen
                  Zusagen. Pro Person genau einmal - gemerkt in
                  `pfand_announced_notified_at`.
  Frist + 3 Tage  Die Zusage wird automatisch ausgetragen: `pfand_announced`
                  zurueck auf False, Frist und Merker geleert. Die gelbe
                  Markierung verschwindet damit aus der Uebersicht.

Warum ein Merker PRO PERSON und keine globale "zuletzt geprueft"-Spalte:
`pfand_announced_notified_at` macht den Durchlauf von selbst idempotent - wer
gemeldet wurde, faellt aus der Abfrage. Eine globale Tagesmarke muesste
zusaetzlich gepflegt werden und wuerde bei zwei parallelen Requests (Render
faehrt mehrere Worker) zu doppelten Mails oder verschluckten Meldungen fuehren.

Bewusst NICHT beruehrt: das Notizfeld. Das gehoert dem Team.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from . import models
from .config import settings
from .email_sender import send_in_background, send_mail


LOCAL_TZ = ZoneInfo("Europe/Berlin")

# Tage nach Fristende, bis die Zusage automatisch ausgetragen wird.
# Beispiel: Frist Montag -> Austragen am Donnerstag.
GRACE_DAYS = 3


def today_local():
    """Heutiges Datum in lokaler Zeit - nicht UTC.

    Wichtig fuer die Tagesgrenze: der Server laeuft in UTC, eine Frist "bis
    Montag" soll aber nach deutschem Kalender ablaufen.
    """
    return datetime.now(LOCAL_TZ).date()


def _overdue_query(db: Session, today):
    """Zusage ist abgelaufen, Geld nicht da, Meldung noch nicht raus."""
    return db.query(models.Helper).filter(
        models.Helper.pfand_announced.is_(True),
        models.Helper.pfand_paid.is_(False),
        models.Helper.pfand_announced_due.isnot(None),
        models.Helper.pfand_announced_due < today,
        models.Helper.pfand_announced_notified_at.is_(None),
    )


def _expired_query(db: Session, today):
    """Frist + GRACE_DAYS erreicht -> Zusage austragen.

    `due <= today - GRACE_DAYS` ist die Kante: bei Frist Montag greift es am
    Donnerstag, nicht schon am Mittwoch.
    """
    return db.query(models.Helper).filter(
        models.Helper.pfand_announced.is_(True),
        models.Helper.pfand_announced_due.isnot(None),
        models.Helper.pfand_announced_due <= today - timedelta(days=GRACE_DAYS),
    )


def _format_person(helper) -> str:
    due = helper.pfand_announced_due
    due_str = due.strftime("%d.%m.%Y") if due else "ohne Frist"
    return f"- {helper.first_name} {helper.last_name} <{helper.email}>, zugesagt bis {due_str}"


def build_digest(overdue: list, expired: list) -> tuple[str, str]:
    """Baut Betreff und Text der Sammelmail."""
    parts = []
    if overdue:
        parts.append(
            "Frist überschritten – das Pfand ist noch nicht eingegangen:\n"
            + "\n".join(_format_person(h) for h in overdue)
        )
    if expired:
        parts.append(
            f"Automatisch ausgetragen (Frist + {GRACE_DAYS} Tage). Die gelbe "
            "Markierung ist entfernt, das Pfand gilt wieder als offen:\n"
            + "\n".join(_format_person(h) for h in expired)
        )

    body = (
        "Automatische Meldung aus dem Helfer-Tool.\n\n"
        + "\n\n".join(parts)
        + "\n\nDetails zu den Fristen stehen in den Notizen der jeweiligen Person."
    )
    subject = f"Pfand-Zusagen: {len(overdue)} überfällig, {len(expired)} ausgetragen"
    return subject, body


def run_sweep(db: Session, background_tasks=None) -> dict:
    """Fuehrt den Durchlauf aus. Gibt zurueck, was passiert ist.

    Wirft nicht: Aufrufer sind normale Seitenaufrufe, die davon nichts merken
    sollen. Mailfehler landen ueber send_in_background im Log.
    """
    today = today_local()

    # Reihenfolge: erst einsammeln, dann schreiben. Wer heute gemeldet UND
    # ausgetragen wird (z.B. weil vier Tage niemand eingeloggt war), taucht
    # bewusst in beiden Abschnitten auf - das ist die ehrlichere Meldung.
    overdue = _overdue_query(db, today).all()
    expired = _expired_query(db, today).all()

    if not overdue and not expired:
        return {"notified": 0, "cleared": 0}

    now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    subject, body = build_digest(overdue, expired)

    for helper in overdue:
        helper.pfand_announced_notified_at = now

    for helper in expired:
        helper.pfand_announced = False
        helper.pfand_announced_due = None
        helper.pfand_announced_notified_at = None

    db.commit()

    # Erst committen, dann senden: lieber eine Mail zu wenig als eine Zusage,
    # die gemeldet wurde und trotzdem gelb bleibt.
    recipient = (settings.HELFEN_EMAIL or "").strip()
    if recipient:
        send_in_background(
            background_tasks, send_mail, [recipient], subject, body,
            label="pfand-promises", bcc=False,
        )

    return {"notified": len(overdue), "cleared": len(expired)}


def run_sweep_safe(db: Session, background_tasks=None) -> dict:
    """run_sweep, aber schluckt alles. Fuer den Aufruf aus Seitenrouten."""
    try:
        return run_sweep(db, background_tasks)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[pfand-promises] Durchlauf fehlgeschlagen: {exc}")
        return {"notified": 0, "cleared": 0}
