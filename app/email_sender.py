"""Mail-Versand via SMTP (optional).

Wenn keine SMTP-Credentials konfiguriert sind, wirft die Funktion eine Exception.
Der Mail-Verteiler im Admin-UI unterstützt auch ohne SMTP Copy/CSV-Export.
"""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Iterable

from .config import settings


class MailError(RuntimeError):
    pass


def render_template(template: str, variables: dict[str, str]) -> str:
    """Einfache Platzhalter-Ersetzung: {{Name}} wird durch variables['Name'] ersetzt."""
    out = template
    for key, val in variables.items():
        out = out.replace("{{" + key + "}}", val or "")
        out = out.replace("{" + key + "}", val or "")  # auch {Name} erlauben
    return out


def send_mail(to_addresses: list[str], subject: str, body: str, bcc: bool = True) -> int:
    """Sendet eine Mail an eine Liste von Empfängern.

    Per Default als BCC, damit die Empfänger sich nicht gegenseitig sehen.
    Gibt die Anzahl erfolgreich versendeter Mails zurück.
    """
    if not settings.smtp_enabled:
        raise MailError("SMTP ist nicht konfiguriert. Bitte SMTP_HOST/USER/PASSWORD in .env setzen.")

    if not to_addresses:
        return 0

    sender = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_ADDRESS))

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["Subject"] = subject
    if bcc:
        # Ein sichtbarer To: an den Absender selbst, echte Empfänger via BCC
        msg["To"] = sender
    else:
        msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        if settings.SMTP_USE_TLS:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)

        # Beim BCC: wir müssen die Empfänger einzeln ans server.sendmail() geben
        recipients = to_addresses if bcc else to_addresses
        server.sendmail(settings.SMTP_FROM_ADDRESS, recipients, msg.as_string())
        server.quit()
        return len(recipients)
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"SMTP-Fehler: {exc}") from exc


def send_personalized(helpers_and_bodies: Iterable[tuple[str, str, str]], subject: str) -> int:
    """Versendet personalisierte Einzelmails.

    helpers_and_bodies: Iterable aus (email, first_name, body) — body ist bereits fertig gerendert.
    """
    if not settings.smtp_enabled:
        raise MailError("SMTP ist nicht konfiguriert.")

    sender = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_ADDRESS))
    sent = 0

    if settings.SMTP_USE_TLS:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        server.starttls()
    else:
        server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
    try:
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        for email, first_name, body in helpers_and_bodies:
            msg = MIMEMultipart()
            msg["From"] = sender
            msg["To"] = formataddr((first_name, email))
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))
            server.sendmail(settings.SMTP_FROM_ADDRESS, [email], msg.as_string())
            sent += 1
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass

    return sent


# ---------------------------------------------------------------------------
# Transaktionale Benachrichtigungen (Reset / Swap)
# ---------------------------------------------------------------------------
def _send_single(to_email: str, to_name: str, subject: str, body: str) -> None:
    """Interner Helfer: eine Mail senden. Wirft MailError bei Problemen."""
    if not settings.smtp_enabled:
        raise MailError("SMTP nicht konfiguriert.")
    sender = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_ADDRESS))
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = formataddr((to_name, to_email))
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        if settings.SMTP_USE_TLS:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.SMTP_FROM_ADDRESS, [to_email], msg.as_string())
        server.quit()
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"SMTP-Fehler: {exc}") from exc


def send_password_reset_email(helper, reset_url: str) -> None:
    subject = f"Passwort zurücksetzen – {settings.FESTIVAL_NAME}"
    body = (
        f"Hallo {helper.first_name},\n\n"
        f"du hast angefragt, dein Passwort fürs {settings.FESTIVAL_NAME} Helfer-Tool zurückzusetzen.\n\n"
        f"Klick auf diesen Link, um ein neues Passwort zu setzen (gilt 2 Stunden):\n\n"
        f"{reset_url}\n\n"
        f"Warst du das nicht? Einfach ignorieren – dann passiert nichts.\n\n"
        f"Liebe Grüße\nDas Helfer-Team"
    )
    _send_single(helper.email, helper.first_name, subject, body)


def send_verification_email(helper, verify_url: str) -> None:
    subject = f"Bitte bestätige deine Email – {settings.FESTIVAL_NAME}"
    body = (
        f"Hallo {helper.first_name},\n\n"
        f"vielen Dank für deine Anmeldung als Helfer:in beim {settings.FESTIVAL_NAME}!\n\n"
        f"Bitte bestätige deine Email-Adresse, indem du auf den folgenden Link klickst:\n\n"
        f"{verify_url}\n\n"
        f"Wenn du dich nicht angemeldet hast, ignoriere diese Mail einfach.\n\n"
        f"Liebe Grüße\nDas Helfer-Team"
    )
    _send_single(helper.email, helper.first_name, subject, body)


def send_swap_request_email(to_helper, from_helper, assignment, message: str | None) -> None:
    shift = assignment.shift
    subject = f"Tausch-Anfrage von {from_helper.first_name} – {settings.FESTIVAL_NAME}"
    body_parts = [
        f"Hallo {to_helper.first_name},",
        "",
        f"{from_helper.first_name} {from_helper.last_name} möchte dir eine Schicht abgeben:",
        "",
        f"  Bereich:  {shift.area.name}",
        f"  Tag:      {shift.day.label}",
        f"  Zeit:     {shift.time_range}",
    ]
    if assignment.role:
        body_parts.append(f"  Rolle:    {assignment.role.name}")
    if message:
        body_parts += ["", f"Nachricht von {from_helper.first_name}:", message]
    body_parts += [
        "",
        "Einloggen und entscheiden (Annehmen/Ablehnen):",
        "Einfach im Helfer-Tool unter 'Mein Bereich' anschauen.",
        "",
        "Liebe Grüße\nDas Helfer-Team",
    ]
    _send_single(to_helper.email, to_helper.first_name, subject, "\n".join(body_parts))


def send_swap_accepted_email(from_helper, by_helper, assignment) -> None:
    shift = assignment.shift
    subject = f"Deine Schicht wurde übernommen – {settings.FESTIVAL_NAME}"
    body = (
        f"Hallo {from_helper.first_name},\n\n"
        f"{by_helper.first_name} {by_helper.last_name} hat deine Tausch-Anfrage angenommen.\n\n"
        f"Die Schicht ({shift.area.name}, {shift.day.label} {shift.time_range}) "
        f"ist jetzt {by_helper.first_name} zugewiesen und du bist raus.\n\n"
        f"Liebe Grüße\nDas Helfer-Team"
    )
    _send_single(from_helper.email, from_helper.first_name, subject, body)


def send_swap_taken_email(from_helper, by_helper, assignment) -> None:
    shift = assignment.shift
    subject = f"Deine Board-Schicht wurde übernommen – {settings.FESTIVAL_NAME}"
    body = (
        f"Hallo {from_helper.first_name},\n\n"
        f"{by_helper.first_name} {by_helper.last_name} hat dir die Schicht abgenommen, "
        f"die du aufs Board gestellt hattest:\n\n"
        f"  {shift.area.name} · {shift.day.label} · {shift.time_range}\n\n"
        f"Du musst jetzt nichts weiter tun.\n\n"
        f"Liebe Grüße\nDas Helfer-Team"
    )
    _send_single(from_helper.email, from_helper.first_name, subject, body)
