"""CSV-Import für den Mail-Reiter: viele individuelle Mails auf einmal.

Der bestehende Mail-Reiter schickt EINEN Text an viele Leute (mit
Platzhaltern). Hier geht es um den umgekehrten Fall: jede Person bekommt
einen eigenen Betreff und einen eigenen Text, vorbereitet in einer
Tabelle.

Spalten (Kopfzeile Pflicht, Reihenfolge egal):
    empfaenger   eine oder mehrere Adressen, getrennt mit |
    betreff      Betreffzeile
    text         Mailtext, siehe Formatierung unten

Der Trenner der Datei (`,` `;` oder Tab) wird erkannt. Adressen werden
IMMER mit `|` getrennt, damit das unabhängig vom Dateitrenner
funktioniert. Mehrere Adressen in einer Zeile landen gemeinsam im
To-Feld (eine Mail, alle sehen sich) - gedacht für Leute mit zwei
Konten.

Formatierung im Textfeld (bewusst winzig gehalten):
    Leerzeile        neuer Absatz
    "* " am Zeilen-  Aufzählungspunkt
      anfang
    **fett**         fett

Daraus entstehen zwei Fassungen: reiner Text und HTML. Wer nur Text
verschicken kann, nimmt die erste und verliert nichts ausser der Optik.

Das Modul verschickt nichts selbst. `send_rows` bekommt die
Sendefunktion übergeben - so ist der Versand testbar, ohne dass eine
Mail rausgeht.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html import escape


# Spaltennamen, die wir akzeptieren. Links der Kanon, rechts was sonst
# noch durchgeht - Excel-Exporte und Handgetipptes sind selten einheitlich.
COLUMN_ALIASES = {
    "empfaenger": {"empfaenger", "empfänger", "email", "e-mail", "mail", "to", "an"},
    "betreff": {"betreff", "subject", "titel"},
    "text": {"text", "body", "nachricht", "inhalt"},
}

REQUIRED = ("empfaenger", "betreff", "text")

# Absichtlich lax: wir wollen Tippfehler wie "name@ example.de" oder ein
# fehlendes @ abfangen, nicht RFC 5322 nachbauen. Die echte Prüfung macht
# der Mailserver.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_SUBJECT = 200


@dataclass
class MailRow:
    """Eine versandfertige Zeile."""
    line: int                       # Zeilennummer in der Datei, für Fehlermeldungen
    recipients: list[str]
    subject: str
    text: str                       # reiner Text
    html: str                       # formatierte Fassung

    @property
    def to_display(self) -> str:
        return ", ".join(self.recipients)


@dataclass
class ParseResult:
    rows: list[MailRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------
def _bold_to_html(escaped: str) -> str:
    """**fett** -> <strong>. Läuft NACH dem Escapen, damit kein HTML durchkommt."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def _bold_to_text(raw: str) -> str:
    """**fett** -> fett. In der Textfassung fallen die Sternchen weg."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", raw)


def render_body(raw: str) -> tuple[str, str]:
    """Baut (Text, HTML) aus dem Rohtext einer Zeile.

    Aufzählungen werden zu <ul>, Absätze zu <p>. Die Textfassung behält
    die Sternchen als Listenzeichen, weil sie dort als Aufzählung
    lesbar sind.
    """
    raw = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return "", ""

    text_lines: list[str] = []
    html_parts: list[str] = []
    buffer: list[str] = []      # laufender Absatz
    bullets: list[str] = []     # laufende Liste

    def flush_paragraph():
        if buffer:
            # Einzelner Umbruch bleibt ein Umbruch (<br>), Leerzeile trennt
            # Absätze. Wichtig für Blöcke wie die Bankverbindung, bei denen
            # jede Zeile für sich stehen muss.
            joined = "<br>".join(_bold_to_html(escape(b)) for b in buffer)
            html_parts.append(f"<p>{joined}</p>")
            buffer.clear()

    def flush_bullets():
        if bullets:
            items = "".join(f"<li>{_bold_to_html(escape(b))}</li>" for b in bullets)
            html_parts.append(f"<ul>{items}</ul>")
            bullets.clear()

    for line in raw.split("\n"):
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            flush_bullets()
            text_lines.append("")
            continue

        if stripped.startswith(("* ", "- ")):
            flush_paragraph()
            item = stripped[2:].strip()
            if not item:
                continue        # leerer Aufzählungspunkt: überspringen
            bullets.append(item)
            text_lines.append(f"* {_bold_to_text(item)}")
            continue

        flush_bullets()
        buffer.append(stripped)
        text_lines.append(_bold_to_text(stripped))

    flush_paragraph()
    flush_bullets()

    # Mehrfache Leerzeilen im Text auf eine eindampfen.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(text_lines)).strip()
    return text, "".join(html_parts)


# ---------------------------------------------------------------------------
# Parsen
# ---------------------------------------------------------------------------
def _detect_delimiter(sample: str) -> str:
    """Trenner der ersten Zeile bestimmen. Fällt auf Komma zurück."""
    header = sample.split("\n", 1)[0]
    counts = {d: header.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _normalise_header(name: str) -> str | None:
    key = (name or "").strip().lstrip("\ufeff").lower()
    for canon, aliases in COLUMN_ALIASES.items():
        if key in aliases:
            return canon
    return None


def split_recipients(cell: str) -> list[str]:
    """Adressfeld aufteilen. Trenner ist `|`, Leerzeichen werden entfernt."""
    return [part.strip() for part in (cell or "").split("|") if part.strip()]


def parse_csv(content: str) -> ParseResult:
    """Liest den Dateiinhalt und gibt versandfertige Zeilen + Fehler zurück.

    Es wird NICHT beim ersten Fehler abgebrochen: alle Probleme werden
    gesammelt, damit man die Datei einmal korrigieren kann statt zehnmal.
    """
    result = ParseResult()

    content = (content or "").lstrip("\ufeff")
    if not content.strip():
        result.errors.append("Die Datei ist leer.")
        return result

    delimiter = _detect_delimiter(content)
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)

    try:
        header = next(reader)
    except StopIteration:
        result.errors.append("Die Datei hat keine Kopfzeile.")
        return result

    mapping: dict[str, int] = {}
    for index, name in enumerate(header):
        canon = _normalise_header(name)
        if canon and canon not in mapping:
            mapping[canon] = index

    missing = [c for c in REQUIRED if c not in mapping]
    if missing:
        result.errors.append(
            "Es fehlen Spalten: " + ", ".join(missing)
            + ". Erwartet werden: empfaenger, betreff, text."
        )
        return result

    seen: set[str] = set()

    for line_no, row in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in row):
            continue        # Leerzeile am Dateiende o.ä.

        def cell(name: str) -> str:
            idx = mapping[name]
            return row[idx] if idx < len(row) else ""

        recipients = split_recipients(cell("empfaenger"))
        subject = (cell("betreff") or "").strip()
        text, html = render_body(cell("text"))

        problems = []
        if not recipients:
            problems.append("keine Adresse")
        for addr in recipients:
            if not EMAIL_RE.match(addr):
                problems.append(f"unklare Adresse „{addr}“")
        if not subject:
            problems.append("kein Betreff")
        elif len(subject) > MAX_SUBJECT:
            problems.append(f"Betreff länger als {MAX_SUBJECT} Zeichen")
        if not text:
            problems.append("kein Text")

        for addr in recipients:
            key = addr.lower()
            if key in seen:
                problems.append(f"„{addr}“ kommt mehrfach vor")
            seen.add(key)

        if problems:
            result.errors.append(f"Zeile {line_no}: " + "; ".join(problems))
            continue

        result.rows.append(MailRow(
            line=line_no, recipients=recipients,
            subject=subject, text=text, html=html,
        ))

    if not result.rows and not result.errors:
        result.errors.append("Die Datei enthält keine Zeilen.")

    return result


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------
def send_rows(rows: list[MailRow], sender, test_address: str | None = None) -> dict:
    """Verschickt die Zeilen. `sender` wird aufgerufen als
    sender(recipients, subject, text, html).

    Ist `test_address` gesetzt, gehen ALLE Mails dorthin und der Betreff
    bekommt einen Vermerk, an wen sie eigentlich gegangen wären. So lässt
    sich eine Datei gefahrlos ausprobieren.

    Eine fehlgeschlagene Zeile stoppt den Durchlauf nicht - sonst bliebe
    bei Adresse 3 von 30 der Rest liegen.
    """
    sent = 0
    failed: list[tuple[str, str]] = []

    for row in rows:
        if test_address:
            to = [test_address]
            subject = f"[Test an {row.to_display}] {row.subject}"
        else:
            to = row.recipients
            subject = row.subject

        try:
            sender(to, subject, row.text, row.html)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed.append((row.to_display, str(exc)))

    return {"sent": sent, "failed": failed}
