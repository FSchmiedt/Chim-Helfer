"""Tests für den CSV-Mailimport (app/mail_import.py).

Drei Dinge dürfen nicht schiefgehen:

  1. Eine Mail landet beim falschen Menschen. Deshalb liegt der
     Schwerpunkt auf dem Adressfeld: mehrere Adressen, Dubletten,
     Tippfehler.
  2. Eine kaputte Zeile verschluckt den Rest der Datei. Fehler werden
     gesammelt, nicht geworfen.
  3. Der Text kommt entstellt an - Umlaute, Semikolons im Fliesstext,
     Zeilenumbrueche in Aufzaehlungen.

Es geht nie eine echte Mail raus: `send_rows` bekommt die Sendefunktion
uebergeben, hier ist das eine Liste.

    pytest tests/test_mail_import.py -v
"""
from __future__ import annotations

import csv
import io

import pytest

from app.mail_import import (
    MailRow,
    parse_csv,
    render_body,
    send_rows,
    split_recipients,
)


def make_csv(rows, delimiter=",", header=("empfaenger", "betreff", "text")):
    """Baut eine CSV mit korrektem Quoting - so wie Excel sie schreiben wuerde."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------
def test_paragraphs_become_p_tags():
    text, html = render_body("Erster Absatz.\n\nZweiter Absatz.")
    assert html == "<p>Erster Absatz.</p><p>Zweiter Absatz.</p>"
    assert text == "Erster Absatz.\n\nZweiter Absatz."


def test_bullets_become_list():
    _, html = render_body("Deine Schichten:\n\n* Einlass Sonntag\n* Cleaning Samstag")
    assert "<ul><li>Einlass Sonntag</li><li>Cleaning Samstag</li></ul>" in html


def test_dash_bullets_work_too():
    _, html = render_body("- Erstens\n- Zweitens")
    assert html == "<ul><li>Erstens</li><li>Zweitens</li></ul>"


def test_empty_bullet_is_dropped():
    """Ein alleinstehendes '*' ist ein Tippfehler, kein Aufzaehlungspunkt."""
    _, html = render_body("* Echter Punkt\n* ")
    assert html.count("<li>") == 1


def test_bold_survives_in_both_versions():
    text, html = render_body("Das ist **wichtig**.")
    assert "<strong>wichtig</strong>" in html
    assert text == "Das ist wichtig."


def test_html_is_escaped():
    """Kein durchgereichtes HTML - sonst zerlegt ein '<' die Mail."""
    _, html = render_body("Preis < 100 & steigend")
    assert "&lt;" in html and "&amp;" in html
    assert "<script" not in html.lower()


def test_bold_does_not_smuggle_html():
    _, html = render_body("**<b>fett</b>**")
    assert "<strong>&lt;b&gt;fett&lt;/b&gt;</strong>" == html.replace("<p>", "").replace("</p>", "")


def test_umlauts_survive():
    text, html = render_body("Küchenhilfe, Grüße, Straße")
    assert "Küchenhilfe" in text and "Grüße" in html


def test_single_newline_stays_a_line_break():
    """Ein Umbruch bleibt ein Umbruch, erst die Leerzeile trennt Absätze."""
    _, html = render_body("Erste Zeile\nzweite Zeile")
    assert html == "<p>Erste Zeile<br>zweite Zeile</p>"


def test_bank_details_keep_their_own_lines():
    """Der Grund für die Regel: die Kontodaten dürfen nicht verschmelzen."""
    block = "Kontoinhaber: Chimaera e.V.\nIBAN: DE56 8505 0300 0221 2546 41"
    text, html = render_body(block)
    assert html.count("<br>") == 1
    assert text.count("\n") == 1


def test_empty_body_returns_empty():
    assert render_body("") == ("", "")
    assert render_body("   \n  ") == ("", "")


# ---------------------------------------------------------------------------
# Adressfeld
# ---------------------------------------------------------------------------
def test_single_recipient():
    assert split_recipients("a@b.de") == ["a@b.de"]


def test_two_recipients_with_pipe():
    """Der Fall Marian: eine Person, zwei Konten, eine Mail."""
    assert split_recipients("a@b.de | c@d.de") == ["a@b.de", "c@d.de"]


def test_recipients_ignore_blanks():
    assert split_recipients(" a@b.de ||  ") == ["a@b.de"]


def test_two_recipients_land_in_one_row():
    content = make_csv([("a@b.de|c@d.de", "Betreff", "Hallo")])
    result = parse_csv(content)
    assert result.ok
    assert len(result.rows) == 1
    assert result.rows[0].recipients == ["a@b.de", "c@d.de"]


# ---------------------------------------------------------------------------
# Parsen
# ---------------------------------------------------------------------------
def test_minimal_file_parses():
    content = make_csv([("felix@example.org", "Schichten Chimaera", "Hallo Felix,\n\nbis bald.")])
    result = parse_csv(content)
    assert result.ok, result.errors
    row = result.rows[0]
    assert row.recipients == ["felix@example.org"]
    assert row.subject == "Schichten Chimaera"
    assert "bis bald." in row.text


def test_semicolon_delimiter_is_detected():
    """Deutsches Excel schreibt Semikolon - das muss ohne Zutun gehen."""
    content = make_csv([("a@b.de", "Betreff", "Text")], delimiter=";")
    result = parse_csv(content)
    assert result.ok, result.errors
    assert result.rows[0].subject == "Betreff"


def test_tab_delimiter_is_detected():
    content = make_csv([("a@b.de", "Betreff", "Text")], delimiter="\t")
    assert parse_csv(content).ok


def test_semicolon_inside_text_does_not_split():
    """Semikolon als Dateitrenner UND im Fliesstext - Quoting muss halten."""
    body = "Erstens; zweitens; drittens."
    content = make_csv([("a@b.de", "Betreff", body)], delimiter=";")
    result = parse_csv(content)
    assert result.ok, result.errors
    assert "zweitens" in result.rows[0].text


def test_multiline_body_in_quotes():
    body = "Hallo,\n\n* Einlass Sonntag\n* Cleaning Samstag\n\nGruss"
    content = make_csv([("a@b.de", "Betreff", body)])
    result = parse_csv(content)
    assert result.ok, result.errors
    assert result.rows[0].text.count("*") == 2
    assert "<ul>" in result.rows[0].html


def test_bom_is_stripped():
    """Excel schreibt gern ein BOM vor die Kopfzeile."""
    content = "\ufeff" + make_csv([("a@b.de", "Betreff", "Text")])
    assert parse_csv(content).ok


def test_header_aliases_accepted():
    content = make_csv(
        [("a@b.de", "Betreff", "Text")],
        header=("E-Mail", "Subject", "Nachricht"),
    )
    assert parse_csv(content).ok


def test_column_order_does_not_matter():
    content = make_csv(
        [("Text hier", "a@b.de", "Betreff")],
        header=("text", "empfaenger", "betreff"),
    )
    result = parse_csv(content)
    assert result.ok, result.errors
    assert result.rows[0].recipients == ["a@b.de"]


def test_blank_lines_are_skipped():
    content = make_csv([("a@b.de", "B", "T")]) + "\n\n"
    result = parse_csv(content)
    assert result.ok, result.errors
    assert len(result.rows) == 1


def test_short_row_does_not_crash():
    """Zeile mit zu wenig Spalten: Fehlermeldung statt IndexError."""
    content = "empfaenger,betreff,text\na@b.de,NurBetreff\n"
    result = parse_csv(content)
    assert not result.ok
    assert "Zeile 2" in result.errors[0]


# ---------------------------------------------------------------------------
# Fehler werden gesammelt, nicht geworfen
# ---------------------------------------------------------------------------
def test_missing_column_is_reported():
    content = make_csv([("a@b.de", "B")], header=("empfaenger", "betreff"))
    result = parse_csv(content)
    assert not result.ok
    assert "text" in result.errors[0]


def test_empty_file_is_reported():
    result = parse_csv("")
    assert not result.ok
    assert "leer" in result.errors[0].lower()


def test_header_only_is_reported():
    result = parse_csv("empfaenger,betreff,text\n")
    assert not result.ok


def test_broken_address_is_reported():
    content = make_csv([("kein-at-zeichen", "B", "T")])
    result = parse_csv(content)
    assert not result.ok
    assert "kein-at-zeichen" in result.errors[0]


def test_missing_subject_is_reported():
    content = make_csv([("a@b.de", "", "T")])
    result = parse_csv(content)
    assert not result.ok
    assert "Betreff" in result.errors[0]


def test_missing_text_is_reported():
    content = make_csv([("a@b.de", "B", "")])
    result = parse_csv(content)
    assert not result.ok
    assert "Text" in result.errors[0]


def test_duplicate_address_warns_but_does_not_block():
    """Zweimal dieselbe Adresse ist meist ein Versehen - manchmal Absicht
    (Testdatei). Deshalb Hinweis statt Blockade."""
    content = make_csv([("a@b.de", "B1", "T1"), ("A@B.DE", "B2", "T2")])
    result = parse_csv(content)
    assert result.ok, result.errors
    assert len(result.rows) == 2
    assert len(result.warnings) == 1
    assert "mehr als eine Mail" in result.warnings[0]


def test_no_warnings_when_addresses_are_unique():
    content = make_csv([("a@b.de", "B", "T"), ("c@d.de", "B", "T")])
    assert parse_csv(content).warnings == []


def test_good_rows_survive_a_bad_one():
    content = make_csv([
        ("gut@example.org", "B", "T"),
        ("kaputt", "B", "T"),
        ("auch.gut@example.org", "B", "T"),
    ])
    result = parse_csv(content)
    assert len(result.rows) == 2, "gute Zeilen wurden mitgerissen"
    assert len(result.errors) == 1


def test_all_errors_are_collected_not_just_the_first():
    content = make_csv([("kaputt1", "B", "T"), ("kaputt2", "B", "T")])
    result = parse_csv(content)
    assert len(result.errors) == 2


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------
@pytest.fixture
def outbox():
    box = []

    def fake_sender(to, subject, text, html):
        box.append({"to": to, "subject": subject, "text": text, "html": html})

    fake_sender.box = box
    return fake_sender


def _rows(n=2):
    return [
        MailRow(line=i + 2, recipients=[f"p{i}@example.org"],
                subject=f"Betreff {i}", text=f"Text {i}", html=f"<p>Text {i}</p>")
        for i in range(n)
    ]


def test_send_delivers_every_row(outbox):
    result = send_rows(_rows(3), outbox)
    assert result["sent"] == 3
    assert result["failed"] == []
    assert len(outbox.box) == 3


def test_each_row_keeps_its_own_subject(outbox):
    """Der Unterschied zum bestehenden Mail-Reiter: Betreff pro Zeile."""
    send_rows(_rows(2), outbox)
    assert [m["subject"] for m in outbox.box] == ["Betreff 0", "Betreff 1"]


def test_test_mode_redirects_everything(outbox):
    """Testlauf: nichts geht an echte Adressen."""
    send_rows(_rows(2), outbox, test_address="admin@example.org")
    assert all(m["to"] == ["admin@example.org"] for m in outbox.box)
    assert all("p0@example.org" in m["subject"] or "p1@example.org" in m["subject"]
               for m in outbox.box)


def test_test_mode_keeps_the_body_intact(outbox):
    send_rows(_rows(1), outbox, test_address="admin@example.org")
    assert outbox.box[0]["text"] == "Text 0"


def test_one_failure_does_not_stop_the_rest():
    calls = []

    def flaky(to, subject, text, html):
        calls.append(to)
        if to == ["p1@example.org"]:
            raise RuntimeError("SMTP sagt nein")

    result = send_rows(_rows(3), flaky)
    assert result["sent"] == 2
    assert len(result["failed"]) == 1
    assert "SMTP sagt nein" in result["failed"][0][1]
    assert len(calls) == 3, "nach dem Fehler wurde abgebrochen"


def test_multi_recipient_row_goes_out_once(outbox):
    row = MailRow(line=2, recipients=["a@b.de", "c@d.de"],
                  subject="B", text="T", html="<p>T</p>")
    send_rows([row], outbox)
    assert len(outbox.box) == 1
    assert outbox.box[0]["to"] == ["a@b.de", "c@d.de"]


def test_empty_input_sends_nothing(outbox):
    assert send_rows([], outbox) == {"sent": 0, "failed": []}


# ---------------------------------------------------------------------------
# Durchstich: Datei rein, Mails raus
# ---------------------------------------------------------------------------
def test_full_round_trip(outbox):
    content = make_csv([
        ("adrian@example.org", "Schichten Chimaera",
         "Hallo Adrian,\n\nBei dir wären es:\n\n* Einlass Sonntag\n* Cleaning Samstag\n\nGruß Felix"),
        ("marian.a@example.org|marian.b@example.org", "Schichten Chimaera",
         "Hallo Marian,\n\nzwei Konten - welches behalten?"),
    ], delimiter=";")

    result = parse_csv(content)
    assert result.ok, result.errors

    sent = send_rows(result.rows, outbox)
    assert sent["sent"] == 2
    assert "<ul>" in outbox.box[0]["html"]
    assert outbox.box[0]["text"].startswith("Hallo Adrian,")
    assert len(outbox.box[1]["to"]) == 2
