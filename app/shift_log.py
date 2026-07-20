"""Protokoll aller Schichtänderungen je Helfer:in.

Jede Änderung an einer Zuweisung (Selbst-Eintragung, Selbst-Austragung,
Admin-Zuweisung, Tausch über Board/Anfrage, Admin-Tausch, Schichtlöschung)
schreibt eine Zeile in `shift_change_log`. Bewusst append-only: es wird nie
etwas geändert oder gelöscht, damit die Historie belastbar bleibt.

Wichtig: die Log-Zeile speichert einen **Snapshot** der Schicht (Bereich, Tag,
Zeit, Rolle) als Text. So bleibt die Historie lesbar, auch wenn die Schicht
später gelöscht oder umbenannt wird — `shift_id` wird dann auf NULL gesetzt.

Zeitstempel sind wie überall im Projekt UTC (datetime.utcnow()).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from . import models


# Ab wann wird protokolliert. Alles davor ist nicht rekonstruierbar, deshalb
# zeigen wir das Datum in der Oberfläche an, damit eine leere Historie nicht
# fälschlich als "nie etwas geändert" gelesen wird.
TRACKING_SINCE = date(2026, 7, 20)


# Was ist passiert (aus Sicht der Helfer:in)
ACTION_LABELS = {
    "assigned": "eingetragen",
    "unassigned": "ausgetragen",
    "role_changed": "Rolle geändert",
}

# Wie/durch wen kam die Änderung zustande
SOURCE_LABELS = {
    "self_signup": "selbst eingetragen",
    "self_withdraw": "selbst ausgetragen",
    "admin": "durch Orga",
    "admin_swap": "Tausch durch Orga",
    "swap_board": "Tausch über Board",
    "swap_request": "Tausch-Anfrage",
    "shift_deleted": "Schicht gelöscht",
}


def _shift_snapshot(shift: Optional[models.Shift]) -> dict:
    """Liest Bereich/Tag/Zeit aus der Schicht – defensiv, falls etwas fehlt."""
    if shift is None:
        return {"area_name": None, "day_label": None, "time_text": None}
    try:
        area_name = shift.area.name if shift.area else None
    except Exception:  # noqa: BLE001 – abgelöste Session o.ä.
        area_name = None
    try:
        day_label = shift.day.label if shift.day else None
    except Exception:  # noqa: BLE001
        day_label = None
    try:
        time_text = shift.time_range
    except Exception:  # noqa: BLE001
        time_text = None
    return {"area_name": area_name, "day_label": day_label, "time_text": time_text}


def log_shift_change(
    db: Session,
    *,
    helper_id: int,
    shift: Optional[models.Shift],
    action: str,
    source: str,
    role=None,
    counterpart_helper_id: int | None = None,
    note: str | None = None,
) -> models.ShiftChangeLog:
    """Schreibt eine Log-Zeile (ohne commit – das macht der Aufrufer).

    `counterpart_helper_id` ist bei Tauschvorgängen die andere beteiligte
    Person, damit man in der Historie sieht, mit wem getauscht wurde.
    """
    snap = _shift_snapshot(shift)
    role_name = None
    if role is not None:
        try:
            role_name = role.name
        except Exception:  # noqa: BLE001
            role_name = None

    entry = models.ShiftChangeLog(
        helper_id=helper_id,
        shift_id=shift.id if shift is not None else None,
        action=action,
        source=source,
        counterpart_helper_id=counterpart_helper_id,
        role_name=role_name,
        note=note,
        created_at=datetime.utcnow(),
        **snap,
    )
    db.add(entry)
    return entry


def log_assignment(
    db: Session,
    assignment: models.ShiftAssignment,
    *,
    action: str,
    source: str,
    counterpart_helper_id: int | None = None,
    note: str | None = None,
) -> models.ShiftChangeLog:
    """Bequemer Wrapper: nimmt Helfer:in, Schicht und Rolle aus der Zuweisung.

    Muss aufgerufen werden, SOLANGE die Zuweisung noch existiert (also vor
    einem `db.delete`), weil der Snapshot daraus gebaut wird.
    """
    return log_shift_change(
        db,
        helper_id=assignment.helper_id,
        shift=assignment.shift,
        action=action,
        source=source,
        role=assignment.role,
        counterpart_helper_id=counterpart_helper_id,
        note=note,
    )


def log_transfer(
    db: Session,
    *,
    shift: Optional[models.Shift],
    from_helper_id: int,
    to_helper_id: int,
    source: str,
    role=None,
    note: str | None = None,
) -> None:
    """Eine Schicht wechselt die Person: zwei Zeilen (raus bei A, rein bei B).

    Beide Zeilen verweisen wechselseitig aufeinander, so steht in der Historie
    von A "an B abgegeben" und bei B "von A übernommen".
    """
    log_shift_change(
        db, helper_id=from_helper_id, shift=shift, action="unassigned",
        source=source, role=role, counterpart_helper_id=to_helper_id, note=note,
    )
    log_shift_change(
        db, helper_id=to_helper_id, shift=shift, action="assigned",
        source=source, role=role, counterpart_helper_id=from_helper_id, note=note,
    )


def last_change_map(db: Session) -> dict[int, datetime]:
    """helper_id -> Zeitpunkt der letzten Schichtänderung.

    Eine einzige gruppierte Abfrage statt N+1 – reicht für die Helferliste.
    """
    from sqlalchemy import func

    rows = (
        db.query(models.ShiftChangeLog.helper_id, func.max(models.ShiftChangeLog.created_at))
        .group_by(models.ShiftChangeLog.helper_id)
        .all()
    )
    return {hid: ts for hid, ts in rows if ts is not None}
