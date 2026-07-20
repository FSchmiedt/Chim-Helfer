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


def send_in_background(background_tasks, func, *args, label: str = "mail", **kwargs) -> None:
    """Haengt einen Mailversand als Hintergrund-Task an die Antwort an.

    Warum: SMTP kostet je nach Server 1-2 Sekunden (TCP + TLS + Login + Zustellung),
    im schlimmsten Fall bis zum Timeout. Frueher lief das mitten im Request, die
    Seite kam also erst NACH dem Versand. Jetzt geht die Antwort sofort raus und
    der Versand laeuft danach weiter - auch wenn die Person die Seite schon
    verlassen hat.

    Fehler landen im Log statt beim Nutzer: Zu diesem Zeitpunkt ist die Antwort
    bereits ausgeliefert, wir koennen sie nicht mehr aendern. Bei Reset- und
    Verifikationsmails ist das ohnehin gewollt, damit man ueber die Rueckmeldung
    nicht herausfinden kann, welche Adressen registriert sind.
    """
    def _runner():
        try:
            func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] Mailversand fehlgeschlagen: {exc}")

    if background_tasks is None:      # Fallback, z.B. in Tests
        _runner()
    else:
        background_tasks.add_task(_runner)


def _safe_formataddr(name: str | None, address: str) -> str:
    """Wie email.utils.formataddr, aber mit UTF-8-Fallback für Umlaute/Akzente.

    Standardmäßig versucht formataddr() den Display-Namen als ASCII zu encoden,
    was bei deutschen Umlauten oder Namen wie "Lukáš" mit einem
    UnicodeEncodeError fehlschlägt. Mit charset='utf-8' macht Python
    automatisch RFC-2047-Encoding (=?utf-8?b?...?=) für den Namen.

    Address wird unverändert weitergegeben (Email-Adressen sind ASCII-Only nach RFC).
    """
    if not name:
        return address
    return formataddr((name, address), charset="utf-8")


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

    sender = _safe_formataddr(settings.SMTP_FROM_NAME, settings.SMTP_FROM_ADDRESS)

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
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)

        # Beim BCC: wir müssen die Empfänger einzeln ans server.sendmail() geben
        recipients = to_addresses if bcc else to_addresses
        server.sendmail(settings.SMTP_FROM_ADDRESS, recipients, msg.as_string())
        server.quit()
        return len(recipients)
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"SMTP-Fehler: {exc}") from exc


def _email_looks_ok(address: str) -> bool:
    """Sehr leichte Sanity-Prüfung: ASCII + genau ein @ + nicht-leer auf beiden Seiten.

    SMTP-Server (insbesondere Google Workspace) akzeptieren Unicode-Adressen
    nur eingeschränkt (SMTPUTF8). Wir behandeln Nicht-ASCII-Adressen als kaputt
    und skippen sie, damit der Versand für die anderen weiterläuft.
    """
    if not address:
        return False
    try:
        address.encode("ascii")
    except UnicodeEncodeError:
        return False
    if address.count("@") != 1:
        return False
    local, _, domain = address.partition("@")
    return bool(local) and bool(domain) and "." in domain


def send_personalized(
    helpers_and_bodies: Iterable[tuple[str, str, str]],
    subject: str,
) -> tuple[int, list[tuple[str, str]]]:
    """Versendet personalisierte Einzelmails.

    helpers_and_bodies: Iterable aus (email, first_name, body) — body ist bereits
    fertig gerendert.

    Returns: (anzahl_gesendet, liste_skipped) wo `skipped` Tupel (email, grund) sind.
    Statt zu crashen, wenn eine einzelne Adresse kaputt ist, wird sie geskippt
    und der Versand für die anderen läuft weiter.
    """
    if not settings.smtp_enabled:
        raise MailError("SMTP ist nicht konfiguriert.")

    sender = _safe_formataddr(settings.SMTP_FROM_NAME, settings.SMTP_FROM_ADDRESS)
    sent = 0
    skipped: list[tuple[str, str]] = []

    if settings.SMTP_USE_TLS:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
        server.starttls()
    else:
        server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
    try:
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        for email, first_name, body in helpers_and_bodies:
            # Whitespace und Zero-Width-Zeichen rausputzen, dann prüfen.
            clean_email = (email or "").strip()
            if not _email_looks_ok(clean_email):
                skipped.append((email or "(leer)", "Adresse ungültig (Sonderzeichen oder kein @)"))
                continue
            try:
                msg = MIMEMultipart()
                msg["From"] = sender
                msg["To"] = _safe_formataddr(first_name, clean_email)
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain", "utf-8"))
                server.sendmail(settings.SMTP_FROM_ADDRESS, [clean_email], msg.as_string())
                sent += 1
            except (UnicodeEncodeError, smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPDataError, smtplib.SMTPSenderRefused) as exc:
                # Einzelmail scheitert → loggen + weitermachen.
                print(f"[send_personalized] FAILED {clean_email}: {exc}")
                skipped.append((clean_email, str(exc)[:200]))
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass

    return sent, skipped


# ---------------------------------------------------------------------------
# Transaktionale Benachrichtigungen (Reset / Swap)
# ---------------------------------------------------------------------------
def _send_single(to_email: str, to_name: str, subject: str, body: str,
                 cc: str | None = None,
                 from_email: str | None = None, from_name: str | None = None) -> None:
    """Interner Helfer: eine Mail senden. Wirft MailError bei Problemen.

    Wenn `cc` gesetzt ist, geht eine zusätzliche Kopie an diese Adresse
    (sichtbar im Cc-Header).

    `from_email`/`from_name` überschreiben optional den Standard-Absender
    (z.B. bar@/helfen@ statt des generischen Helfer-Team-Postfachs). Achtung:
    ob das beim jeweiligen Mailprovider ohne SPF/DKIM-Warnung durchgeht, hängt
    davon ab, ob der SMTP-Account eine "Senden als"-Berechtigung für diese
    Adresse hat – ggf. beim Provider (z.B. Google Workspace) einrichten.
    """
    if not settings.smtp_enabled:
        raise MailError("SMTP nicht konfiguriert.")
    sender = _safe_formataddr(from_name or settings.SMTP_FROM_NAME,
                               from_email or settings.SMTP_FROM_ADDRESS)
    envelope_from = from_email or settings.SMTP_FROM_ADDRESS
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = _safe_formataddr(to_name, to_email)
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [to_email]
    if cc and cc.lower() != to_email.lower():
        recipients.append(cc)

    try:
        if settings.SMTP_USE_TLS:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(envelope_from, recipients, msg.as_string())
        server.quit()
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"SMTP-Fehler: {exc}") from exc


def deliver(prepared: dict) -> None:
    """Verschickt eine vorbereitete Nachricht. Braucht KEINE DB-Session mehr.

    Genau deshalb getrennt: die build_*-Funktionen lesen ORM-Objekte (und damit
    Lazy-Loads wie assignment.shift.area.name) und muessen im Request laufen,
    solange die Session offen ist. Nur dieses reine String-Versenden wandert in
    den Hintergrund-Task.
    """
    _send_single(prepared["to_email"], prepared["to_name"], prepared["subject"],
                 prepared["body"], cc=prepared.get("cc"),
                 from_email=prepared.get("from_email"), from_name=prepared.get("from_name"))


def build_password_reset_message(helper, reset_url: str) -> dict:
    return {
        "to_email": helper.email,
        "to_name": helper.first_name,
        "subject": f"Passwort zurücksetzen – {settings.FESTIVAL_NAME}",
        "body": (
            f"Hallo {helper.first_name},\n\n"
            f"du hast angefragt, dein Passwort fürs {settings.FESTIVAL_NAME} Helfer-Tool zurückzusetzen.\n\n"
            f"Klick auf diesen Link, um ein neues Passwort zu setzen (gilt 2 Stunden):\n\n"
            f"{reset_url}\n\n"
            f"Warst du das nicht? Einfach ignorieren – dann passiert nichts.\n\n"
            f"Liebe Grüße\nDas Helfer-Team"
        ),
    }


def send_password_reset_email(helper, reset_url: str) -> None:
    deliver(build_password_reset_message(helper, reset_url))


def build_verification_message(helper, verify_url: str, cc: str | None = None) -> dict:
    return {
        "to_email": helper.email,
        "to_name": helper.first_name,
        "cc": cc,
        "subject": f"Bitte bestätige deine Email – {settings.FESTIVAL_NAME}",
        "body": (
            f"Hallo {helper.first_name},\n\n"
            f"vielen Dank für deine Anmeldung als Helfer:in beim {settings.FESTIVAL_NAME}!\n\n"
            f"Bitte bestätige deine Email-Adresse, indem du auf den folgenden Link klickst:\n\n"
            f"{verify_url}\n\n"
            f"Wenn du dich nicht angemeldet hast, ignoriere diese Mail einfach.\n\n"
            f"Liebe Grüße\nDas Helfer-Team"
        ),
    }


def send_verification_email(helper, verify_url: str, cc: str | None = None) -> None:
    deliver(build_verification_message(helper, verify_url, cc))


def build_swap_request_message(to_helper, from_helper, assignment, message: str | None) -> dict:
    shift = assignment.shift
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
    return {
        "to_email": to_helper.email,
        "to_name": to_helper.first_name,
        "subject": f"Tausch-Anfrage von {from_helper.first_name} – {settings.FESTIVAL_NAME}",
        "body": "\n".join(body_parts),
    }


def send_swap_request_email(to_helper, from_helper, assignment, message: str | None) -> None:
    deliver(build_swap_request_message(to_helper, from_helper, assignment, message))


def build_swap_accepted_message(from_helper, by_helper, assignment) -> dict:
    shift = assignment.shift
    return {
        "to_email": from_helper.email,
        "to_name": from_helper.first_name,
        "subject": f"Deine Schicht wurde übernommen – {settings.FESTIVAL_NAME}",
        "body": (
            f"Hallo {from_helper.first_name},\n\n"
            f"{by_helper.first_name} {by_helper.last_name} hat deine Tausch-Anfrage angenommen.\n\n"
            f"Die Schicht ({shift.area.name}, {shift.day.label} {shift.time_range}) "
            f"ist jetzt {by_helper.first_name} zugewiesen und du bist raus.\n\n"
            f"Liebe Grüße\nDas Helfer-Team"
        ),
    }


def send_swap_accepted_email(from_helper, by_helper, assignment) -> None:
    deliver(build_swap_accepted_message(from_helper, by_helper, assignment))


def build_swap_taken_message(from_helper, by_helper, assignment) -> dict:
    shift = assignment.shift
    return {
        "to_email": from_helper.email,
        "to_name": from_helper.first_name,
        "subject": f"Deine Board-Schicht wurde übernommen – {settings.FESTIVAL_NAME}",
        "body": (
            f"Hallo {from_helper.first_name},\n\n"
            f"{by_helper.first_name} {by_helper.last_name} hat dir die Schicht abgenommen, "
            f"die du aufs Board gestellt hattest:\n\n"
            f"  {shift.area.name} · {shift.day.label} · {shift.time_range}\n\n"
            f"Du musst jetzt nichts weiter tun.\n\n"
            f"Liebe Grüße\nDas Helfer-Team"
        ),
    }


def send_swap_taken_email(from_helper, by_helper, assignment) -> None:
    deliver(build_swap_taken_message(from_helper, by_helper, assignment))


# ---------------------------------------------------------------------------
# Org-Benachrichtigung bei Schicht-Austragung (Bar -> bar@, sonst -> helfen@)
# ---------------------------------------------------------------------------
def _org_contact_for_area(area) -> tuple[str, str]:
    """Gibt (email, name) des zustaendigen Org-Postfachs fuer einen Bereich zurueck."""
    is_bar = area.name.strip().lower() in settings.swap_excluded_areas
    if is_bar:
        return settings.BAR_EMAIL, settings.BAR_NAME
    return settings.HELFEN_EMAIL, settings.HELFEN_NAME


def build_org_withdraw_notice(helper, shift) -> dict:
    """An bar@/helfen@, wenn sich jemand selbst aus einer Schicht austraegt.

    Gilt fuer alle Bereiche gleich, auch Bar: die Schicht ist im Tool schon
    wieder frei, das hier ist reine Info fuer die Orga, damit niemand jede
    Austragung einzeln im Tool nachschauen muss.
    """
    org_email, org_name = _org_contact_for_area(shift.area)
    who = f"{helper.first_name} {helper.last_name} ({helper.email})"
    where = (
        f"  Bereich:  {shift.area.name}\n"
        f"  Tag:      {shift.day.label}\n"
        f"  Zeit:     {shift.time_range}\n"
    )
    subject = f"Schicht-Austragung – {helper.first_name} {helper.last_name}"
    body = (
        f"Hallo,\n\n"
        f"{who} hat sich selbst aus folgender Schicht ausgetragen:\n\n"
        f"{where}\n"
        f"Die Schicht ist im Tool automatisch wieder frei - reine Info, kein "
        f"Handlungsbedarf.\n\n"
        f"Liebe Grüße\nDas Helfer-Tool"
    )
    return {
        "to_email": org_email,
        "to_name": org_name,
        "subject": subject,
        "body": body,
    }


def send_org_withdraw_notice(helper, shift) -> None:
    deliver(build_org_withdraw_notice(helper, shift))


# ---------------------------------------------------------------------------
# Benachrichtigung an Helfer:in, wenn Admin sie/ihn ein-/austraegt
# ---------------------------------------------------------------------------
def build_shift_change_notice_for_helper(helper, shift, action: str, role=None) -> dict:
    """action: 'assigned' oder 'unassigned'. Geht an die/den betroffene:n Helfer:in."""
    verb = "eingetragen" if action == "assigned" else "ausgetragen"
    subject = f"Du wurdest {verb} – {settings.FESTIVAL_NAME}"
    role_line = f"  Rolle:    {role.name}\n" if role else ""
    body = (
        f"Hallo {helper.first_name},\n\n"
        f"das Orga-Team hat dich gerade {verb}:\n\n"
        f"  Bereich:  {shift.area.name}\n"
        f"  Tag:      {shift.day.label}\n"
        f"  Zeit:     {shift.time_range}\n"
        f"{role_line}\n"
        f"Schau bei Fragen gern unter 'Mein Bereich' im Helfer-Tool vorbei.\n\n"
        f"Liebe Grüße\nDas Helfer-Team"
    )
    return {
        "to_email": helper.email,
        "to_name": helper.first_name,
        "subject": subject,
        "body": body,
    }


def send_shift_change_notice_to_helper(helper, shift, action: str, role=None) -> None:
    deliver(build_shift_change_notice_for_helper(helper, shift, action, role))


# ---------------------------------------------------------------------------
# 75€-Ein-Schicht-Angebot (manuell vom Admin ausgeloest)
# ---------------------------------------------------------------------------
def build_discount_offer_message(helper) -> dict:
    """Info an eine:n Helfer:in, dass sie/er nur fuer eine einzelne Schicht

    (75€ effektiv, nach Teil-Rueckerstattung vom vollen 160€-Pfand) eingetragen
    wurde. Kein Angebot, sondern eine bereits erfolgte Entscheidung - die
    Person soll sich nur melden, falls sie NICHT einverstanden ist. Absender
    ist bewusst bar@/helfen@ (nicht das generische Helfer-Team-Postfach),
    damit Antworten direkt bei der richtigen Stelle landen. Bereich wird ueber
    die aktuellen Zuweisungen der Person bestimmt (erste gefundene
    Bar-Zuweisung zaehlt); ohne Zuweisung geht's an helfen@.
    """
    is_bar = any(
        a.shift.area.name.strip().lower() in settings.swap_excluded_areas
        for a in helper.shift_assignments
    )
    org_email = settings.BAR_EMAIL if is_bar else settings.HELFEN_EMAIL
    org_name = settings.BAR_NAME if is_bar else settings.HELFEN_NAME

    subject = f"Info: nur eine Schicht für dich eingetragen (75€) – {settings.FESTIVAL_NAME}"
    body = (
        f"Hallo {helper.first_name},\n\n"
        f"wir haben dich für nur eine einzelne Schicht eingetragen. Am Pfand "
        f"ändert das nichts - das wären erstmal 160€ Pfand, du bekommst am "
        f"Ende aber 85€ davon zurück, sodass du effektiv nur 75€ für den "
        f"Festivalbesuch mit nur einer Schicht Verantwortung gezahlt hast.\n"
        f"Falls du damit nicht einverstanden bist, schreib uns gerne kurz "
        f"eine Mail an {org_email}.\n\n"
        f"Liebe Grüße\n{org_name}"
    )
    return {
        "to_email": helper.email,
        "to_name": helper.first_name,
        "from_email": org_email,
        "from_name": org_name,
        "subject": subject,
        "body": body,
    }


def send_discount_offer_email(helper) -> None:
    deliver(build_discount_offer_message(helper))
