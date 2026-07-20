"""Zentrale Prüfregeln fürs Zuweisen von Schichten.

Eine Stelle für alle Regeln, damit Admin-Zuweisung und Admin-Tausch nicht
auseinanderlaufen. Die Funktionen geben Verstöße *zurück*, statt sie zu
erzwingen — die aufrufende Route entscheidet, ob sie blockiert (Helfende) oder
nur warnt und übersteuern lässt (Admin).

Jeder Verstoß ist ein dict mit:
    code            technischer Schlüssel ("overlap" | "rest" | "over_soll")
    headline        kurze fette Zeile für die rote Box
    detail          konkrete Erklärung mit Namen, Zeiten, Bereich
    override_label  Satzteil für den "trotzdem"-Button, bewusst deutlich
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import settings


def _span(shift) -> tuple[datetime, datetime]:
    """Absolute Start-/Endzeit einer Schicht.

    Schichten über Mitternacht (z.B. 22:00-04:00) haben end_time < start_time.
    Für die Ruhezeit brauchen wir echte Zeitpunkte, sonst rechnen wir Unsinn.
    """
    base = shift.day.date
    start = datetime.combine(base, shift.start_time)
    end = datetime.combine(base, shift.end_time)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _fmt(shift) -> str:
    return (f"{shift.area.name} · {shift.day.label} · "
            f"{shift.start_time.strftime('%H:%M')}-{shift.end_time.strftime('%H:%M')}")


def soll_for(helper) -> int:
    """Wie viele Schichten soll diese Person machen?"""
    return 1 if helper.wants_only_one_shift else settings.MIN_SHIFTS


def check_assignment(helper, shift, other_assignments) -> list[dict]:
    """Prüft, ob `helper` die `shift` bekommen darf.

    `other_assignments` sind alle bestehenden Zuweisungen der Person OHNE die,
    die gerade ersetzt wird (beim Tausch wichtig, sonst prüft man gegen sich
    selbst).
    """
    violations: list[dict] = []
    new_start, new_end = _span(shift)
    name = f"{helper.first_name} {helper.last_name}"

    # --- 1. Zeitliche Überschneidung ---------------------------------------
    for other in other_assignments:
        o_start, o_end = _span(other.shift)
        if o_start < new_end and new_start < o_end:
            violations.append({
                "code": "overlap",
                "headline": "DOPPELSCHICHT — die Zeiten überschneiden sich",
                "detail": (f"{name} macht zu dieser Zeit bereits {_fmt(other.shift)}. "
                           f"Beide Schichten gleichzeitig geht nicht."),
                "override_label": "eine DOPPELSCHICHT arbeiten lassen",
            })
            break

    # --- 2. Ruhezeit --------------------------------------------------------
    min_rest = timedelta(hours=settings.MIN_REST_HOURS)
    for other in other_assignments:
        o_start, o_end = _span(other.shift)
        if o_start < new_end and new_start < o_end:
            continue  # echte Überschneidung, oben schon gemeldet
        gap = new_start - o_end if new_start >= o_end else o_start - new_end
        if gap < min_rest:
            hours = gap.total_seconds() / 3600
            std = "STUNDE" if 0.5 <= hours < 1.5 else "STUNDEN"
            violations.append({
                "code": "rest",
                "headline": f"NUR {hours:.0f} {std} PAUSE — Ruhezeit unterschritten",
                "detail": (f"{name} macht {_fmt(other.shift)}. Dazwischen liegen nur "
                           f"{hours:.1f} Stunden, gefordert sind {settings.MIN_REST_HOURS}."),
                "override_label": f"NACH NUR {hours:.0f} {std} PAUSE wieder arbeiten lassen",
            })
            break

    # --- 3. Mehr als das eigene Soll ---------------------------------------
    soll = soll_for(helper)
    kuenftig = len(other_assignments) + 1
    if kuenftig > soll:
        violations.append({
            "code": "over_soll",
            "headline": f"MEHR ALS DAS SOLL — {kuenftig} statt {soll} Schichten",
            "detail": (f"{name} hat {soll} Schicht(en) angegeben und käme mit dieser "
                       f"auf {kuenftig}."
                       + (" Achtung: Ein-Schicht-Ticket (75 €)."
                          if helper.wants_only_one_shift else "")),
            "override_label": (f"MEHR ALS DIE {soll} ANGEGEBENE(N) SCHICHT(EN) "
                               f"arbeiten lassen"),
        })

    return violations


def override_sentence(helper, violations: list[dict]) -> str:
    """Text für den Übersteuern-Button — bewusst unmissverständlich."""
    name = f"{helper.first_name} {helper.last_name}"
    if len(violations) == 1:
        return f"Ich will {name} trotzdem {violations[0]['override_label']}"
    return (f"Ich will {name} trotzdem einteilen — "
            f"trotz aller {len(violations)} Warnungen")
