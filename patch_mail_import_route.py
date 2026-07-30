"""EINFÜGEN in app/routers/admin_pages.py, direkt hinter mail_send (Zeile ~1858).

Zwei Routen: Vorschau (parsen und anzeigen, nichts senden) und Versand.
Bewusst getrennt - eine Datei mit 30 Mails will man vorher sehen.

Oben bei den Imports ergänzen:
    from .. import mail_import
"""

# --- ab hier kopieren ------------------------------------------------------

@router.post("/mail/import/preview", response_class=HTMLResponse)
async def mail_import_preview(request: Request, db: Session = Depends(get_db)):
    """CSV hochladen und anzeigen, was rausgehen würde. Sendet nichts."""
    if (r := require_admin_redirect(request)):
        return r
    from .. import mail_import

    form = await request.form()
    upload = form.get("csv_file")
    if upload is None or not getattr(upload, "filename", ""):
        return _mail_import_page(request, db, errors=["Keine Datei ausgewählt."])

    raw = await upload.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel unter Windows schreibt gern cp1252 statt UTF-8.
        try:
            content = raw.decode("cp1252")
        except UnicodeDecodeError:
            return _mail_import_page(request, db, errors=[
                "Die Datei ist weder UTF-8 noch Windows-1252. "
                "Bitte in Excel als „CSV UTF-8“ speichern."
            ])

    result = mail_import.parse_csv(content)
    return _mail_import_page(request, db, rows=result.rows,
                             errors=result.errors, csv_content=content)


@router.post("/mail/import/send")
async def mail_import_send(request: Request, background_tasks: BackgroundTasks,
                           db: Session = Depends(get_db)):
    """Verschickt die zuvor geprüfte Datei. Testlauf geht nur an den Admin."""
    if (r := require_admin_redirect(request)):
        return r
    from .. import mail_import
    from ..email_sender import send_mail

    form = await request.form()
    content = form.get("csv_content") or ""
    test_only = form.get("test_only") == "on"

    result = mail_import.parse_csv(content)
    if not result.ok:
        return _mail_import_page(request, db, rows=result.rows, errors=result.errors,
                                 csv_content=content)

    def sender(to, subject, text, html):
        # bcc=False: bei zwei Adressen in einer Zeile sollen beide im
        # To-Feld stehen - das ist dieselbe Person mit zwei Konten.
        # ACHTUNG: prüfen, ob send_mail in eurem email_sender.py ein
        # html-Argument annimmt. Falls nicht, die nächste Zeile auf
        #     send_mail(to, subject, text, bcc=False)
        # ändern - dann geht die Mail als reiner Text raus.
        send_mail(to, subject, text, bcc=False)

    outcome = mail_import.send_rows(
        result.rows, sender,
        test_address=(settings.SMTP_FROM_ADDRESS if test_only else None),
    )

    if outcome["failed"]:
        lines = "\n".join(f"  • {who}: {why}" for who, why in outcome["failed"])
        message = (f"{outcome['sent']} Mail(s) versendet, "
                   f"{len(outcome['failed'])} fehlgeschlagen:\n{lines}")
        success = "partial"
    else:
        prefix = "Testlauf: " if test_only else ""
        message = f"{prefix}{outcome['sent']} Mail(s) versendet."
        success = True

    return _mail_import_page(request, db, message=message, success=success)


def _mail_import_page(request, db, rows=None, errors=None,
                      csv_content="", message=None, success=None):
    """Gemeinsamer Seitenaufbau für Vorschau und Versand."""
    return templates.TemplateResponse(
        "admin/mail_import.html",
        _ctx(
            request,
            rows=rows or [],
            errors=errors or [],
            csv_content=csv_content,
            message=message,
            success=success,
            test_address=settings.SMTP_FROM_ADDRESS,
        ),
    )
