"""Datenmodelle – SQLAlchemy 2.0 Style."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


# ---------------------------------------------------------------------------
# Festival-Konfiguration (vom Admin gepflegt)
# ---------------------------------------------------------------------------
class FestivalDay(Base):
    """Ein Festivaltag (z.B. 'Donnerstag Aufbau', 'Freitag')."""
    __tablename__ = "festival_days"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    label: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    availabilities: Mapped[list["Availability"]] = relationship(back_populates="day", cascade="all, delete-orphan")
    shifts: Mapped[list["Shift"]] = relationship(back_populates="day", cascade="all, delete-orphan")


class Area(Base):
    """Ein Einsatzbereich (z.B. 'Bar', 'Einlass', 'Aufbau/Abbau')."""
    __tablename__ = "areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    roles: Mapped[list["Role"]] = relationship(back_populates="area", cascade="all, delete-orphan")
    preferences: Mapped[list["HelperAreaPreference"]] = relationship(back_populates="area", cascade="all, delete-orphan")
    shifts: Mapped[list["Shift"]] = relationship(back_populates="area", cascade="all, delete-orphan")


class Role(Base):
    """Rolle innerhalb eines Bereichs (z.B. Bar -> Barleitung, Springer, ...)."""
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("area_id", "name", name="uq_role_area_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    area: Mapped["Area"] = relationship(back_populates="roles")
    helper_trusts: Mapped[list["HelperRoleTrust"]] = relationship(back_populates="role", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Helfer:innen
# ---------------------------------------------------------------------------
class Helper(Base):
    __tablename__ = "helpers"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Personendaten
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    date_of_birth: Mapped[date] = mapped_column(Date)

    # Auth
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    password_reset_token: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    password_reset_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Email-Verifikation
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    email_verification_token: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)

    # Wann hat die Person zuletzt ihre eigene Schichtübersicht (/me) geöffnet?
    # Ersetzt die unzuverlässige Lesebestätigung: misst tatsächliches Nachschauen.
    last_me_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    me_view_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)

    # Zahlungsdaten für Pfand etc.
    iban: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    paypal: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Pfand-Tracking (nur Admin; True = erledigt)
    pfand_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    pfand_returned: Mapped[bool] = mapped_column(Boolean, default=False)

    # Erfahrung
    been_here_before: Mapped[bool] = mapped_column(Boolean, default=False)
    previous_festivals: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Freitextfelder
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # vom Helfer:in
    admin_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # nur intern

    # Status
    status: Mapped[str] = mapped_column(String(30), default="registered", index=True)
    # registered | confirmed | declined | withdrawn

    # Pfand-Tracking (vom Admin gepflegt)
    pfand_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    pfand_paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pfand_returned: Mapped[bool] = mapped_column(Boolean, default=False)
    pfand_returned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Einwilligungen
    is_adult_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_no_guarantee: Mapped[bool] = mapped_column(Boolean, default=False)

    # Helfer:in möchte nur eine einzige Schicht (statt 2 für Volunteer-Ticket).
    # Bedeutung: zahlt 75€ für ein Ticket. Wird vom Helfer im /me Dashboard gesetzt.
    wants_only_one_shift: Mapped[bool] = mapped_column(Boolean, default=False)

    # Admin bietet (manuell, im Einzelfall) die 75€-Ein-Schicht-Regelung an,
    # statt dass die Person selbst danach fragt. Ausloest beim Setzen eine
    # Mail an die Person (Absender bar@/helfen@ je nach Bereich).
    discount_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    discount_offered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    availabilities: Mapped[list["Availability"]] = relationship(back_populates="helper", cascade="all, delete-orphan")
    preferences: Mapped[list["HelperAreaPreference"]] = relationship(back_populates="helper", cascade="all, delete-orphan")
    tags: Mapped[list["HelperTag"]] = relationship(back_populates="helper", cascade="all, delete-orphan")
    role_trusts: Mapped[list["HelperRoleTrust"]] = relationship(back_populates="helper", cascade="all, delete-orphan")
    shift_assignments: Mapped[list["ShiftAssignment"]] = relationship(back_populates="helper", cascade="all, delete-orphan")
    shift_changes: Mapped[list["ShiftChangeLog"]] = relationship(
        back_populates="helper",
        cascade="all, delete-orphan",
        foreign_keys="ShiftChangeLog.helper_id",
        order_by="ShiftChangeLog.created_at.desc()",
    )

    # Helpers
    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def short_name(self) -> str:
        return f"{self.first_name} {self.last_name[:1]}."

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


class Availability(Base):
    """An welchen Tagen ist Helfer:in verfügbar."""
    __tablename__ = "availabilities"
    __table_args__ = (UniqueConstraint("helper_id", "day_id", name="uq_avail_helper_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"))
    day_id: Mapped[int] = mapped_column(ForeignKey("festival_days.id", ondelete="CASCADE"))

    helper: Mapped["Helper"] = relationship(back_populates="availabilities")
    day: Mapped["FestivalDay"] = relationship(back_populates="availabilities")


class HelperTag(Base):
    """Freie Markierung an einer Helfer:in, z.B. 'zugewiesen'.

    Bewusst als eigene Tabelle statt als Komma-Spalte: so lässt sich sauber
    filtern und eine Markierung gezielt wieder entfernen, ohne Stringgefummel.
    """

    __tablename__ = "helper_tags"
    __table_args__ = (UniqueConstraint("helper_id", "tag", name="uq_helper_tag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"), index=True)
    tag: Mapped[str] = mapped_column(String(50), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    helper: Mapped["Helper"] = relationship(back_populates="tags")


class HelperAreaPreference(Base):
    """Wunschbereiche mit Ranking (1 = erste Wahl)."""
    __tablename__ = "helper_area_preferences"
    __table_args__ = (UniqueConstraint("helper_id", "area_id", name="uq_pref_helper_area"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"))
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer, default=1)  # 1 = top

    helper: Mapped["Helper"] = relationship(back_populates="preferences")
    area: Mapped["Area"] = relationship(back_populates="preferences")


class HelperRoleTrust(Base):
    """Händisch vom Admin gepflegt: welche Rollen traut man welcher Helfer:in zu?

    Beispiel: Anna K. hat Bar als Wunsch und Admin setzt sie auf 'Tresenkraft' + 'Runner'.
    """
    __tablename__ = "helper_role_trusts"
    __table_args__ = (UniqueConstraint("helper_id", "role_id", name="uq_trust_helper_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))

    helper: Mapped["Helper"] = relationship(back_populates="role_trusts")
    role: Mapped["Role"] = relationship(back_populates="helper_trusts")


# ---------------------------------------------------------------------------
# Schichten
# ---------------------------------------------------------------------------
class Shift(Base):
    """Eine konkrete Schicht: Bereich + Tag + Zeitfenster + Kapazität."""
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id", ondelete="CASCADE"))
    day_id: Mapped[int] = mapped_column(ForeignKey("festival_days.id", ondelete="CASCADE"))

    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # z.B. "Hauptbar Schicht 1"
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    capacity: Mapped[int] = mapped_column(Integer, default=1)

    area: Mapped["Area"] = relationship(back_populates="shifts")
    day: Mapped["FestivalDay"] = relationship(back_populates="shifts")
    assignments: Mapped[list["ShiftAssignment"]] = relationship(back_populates="shift", cascade="all, delete-orphan")

    @property
    def time_range(self) -> str:
        return f"{self.start_time.strftime('%H:%M')} – {self.end_time.strftime('%H:%M')}"


class ShiftAssignment(Base):
    """Zuweisung Helfer:in <-> Schicht, optional mit konkreter Rolle."""
    __tablename__ = "shift_assignments"
    __table_args__ = (UniqueConstraint("shift_id", "helper_id", name="uq_assign_shift_helper"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"))
    helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"))
    role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id", ondelete="SET NULL"), nullable=True)

    shift: Mapped["Shift"] = relationship(back_populates="assignments")
    helper: Mapped["Helper"] = relationship(back_populates="shift_assignments")
    role: Mapped[Optional["Role"]] = relationship()


# ---------------------------------------------------------------------------
# Änderungsprotokoll
# ---------------------------------------------------------------------------
class ShiftChangeLog(Base):
    """Append-only Protokoll: wer wurde wann welcher Schicht zu-/abgeordnet.

    Bereich/Tag/Zeit/Rolle werden als Text mitgeschrieben (Snapshot), damit die
    Historie lesbar bleibt, wenn die Schicht später gelöscht wird. `shift_id`
    steht dann auf NULL, die Zeile bleibt aber erhalten.

    Geschrieben wird ausschließlich über `app/shift_log.py`.
    """
    __tablename__ = "shift_change_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    helper_id: Mapped[int] = mapped_column(
        ForeignKey("helpers.id", ondelete="CASCADE"), index=True
    )
    shift_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True
    )

    action: Mapped[str] = mapped_column(String(20), index=True)
    # assigned | unassigned | role_changed
    source: Mapped[str] = mapped_column(String(30))
    # self_signup | self_withdraw | admin | admin_swap | swap_board |
    # swap_request | shift_deleted

    # Bei Tausch: die andere beteiligte Person.
    counterpart_helper_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("helpers.id", ondelete="SET NULL"), nullable=True
    )

    # Snapshot der Schicht zum Zeitpunkt der Änderung
    area_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    day_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    time_text: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    role_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=datetime.utcnow)

    helper: Mapped["Helper"] = relationship(
        back_populates="shift_changes", foreign_keys=[helper_id]
    )
    counterpart: Mapped[Optional["Helper"]] = relationship(
        foreign_keys=[counterpart_helper_id]
    )

    @property
    def shift_text(self) -> str:
        """Einzeilige Beschreibung der betroffenen Schicht."""
        parts = [p for p in (self.area_name, self.day_label, self.time_text) if p]
        base = " · ".join(parts) if parts else "Schicht"
        if self.role_name:
            base = f"{base} ({self.role_name})"
        return base


# ---------------------------------------------------------------------------
# Schichttausch
# ---------------------------------------------------------------------------
class ShiftSwapOffer(Base):
    """Eine Schicht wird öffentlich aufs Board gestellt – für einen 1:1-Tausch.

    Person A stellt ihre Schicht rein und gibt an, welche Gegenschicht(en) sie
    im Tausch akzeptiert:
      - "day": jede Schicht, die an `wanted_day_id` beginnt
      - "shifts": nur die konkret in `wanted_shifts` angehakten Schichten
    Optional erlaubt A auch eine reine Übernahme ohne Gegenschicht
    (`allow_giveaway`).

    Person B übernimmt, indem sie (bei 1:1) eine ihrer passenden Schichten
    hergibt – oder (bei giveaway) gar keine.
    """
    __tablename__ = "shift_swap_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("shift_assignments.id", ondelete="CASCADE"),
        unique=True,  # pro Zuweisung nur ein offenes Angebot
    )
    offered_by_helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"))
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    # open | taken | cancelled

    # Tausch-Präferenz von A:
    #   want_type = "day"    -> wanted_day_id gesetzt, jede Schicht an dem Tag ok
    #   want_type = "shifts" -> wanted_shifts enthält die konkreten Wunschschichten
    want_type: Mapped[str] = mapped_column(String(10), default="day")  # "day" | "shifts"
    wanted_day_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("festival_days.id", ondelete="CASCADE"), nullable=True
    )
    # Erlaubt A zusätzlich, dass jemand die Schicht einfach nur übernimmt
    # (ohne im Gegenzug eine Schicht abzugeben).
    allow_giveaway: Mapped[bool] = mapped_column(Boolean, default=False)

    taken_by_helper_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("helpers.id", ondelete="SET NULL"), nullable=True
    )
    # Welche Schicht B im Gegenzug abgegeben hat (NULL bei reiner Übernahme).
    taken_with_assignment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shift_assignments.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    assignment: Mapped["ShiftAssignment"] = relationship(foreign_keys=[assignment_id])
    offered_by: Mapped["Helper"] = relationship(foreign_keys=[offered_by_helper_id])
    taken_by: Mapped[Optional["Helper"]] = relationship(foreign_keys=[taken_by_helper_id])
    wanted_day: Mapped[Optional["FestivalDay"]] = relationship()
    wanted_shifts: Mapped[list["ShiftSwapOfferWantedShift"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )


class ShiftSwapOfferWantedShift(Base):
    """Konkrete Wunschschicht eines Board-Angebots (bei want_type='shifts')."""
    __tablename__ = "shift_swap_offer_wanted_shifts"
    __table_args__ = (
        UniqueConstraint("offer_id", "shift_id", name="uq_offer_wanted_shift"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("shift_swap_offers.id", ondelete="CASCADE"))
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"))

    offer: Mapped["ShiftSwapOffer"] = relationship(back_populates="wanted_shifts")
    shift: Mapped["Shift"] = relationship()


class ShiftSwapRequest(Base):
    """Direkte Tausch-Anfrage an eine konkrete andere Helfer:in.

    Flow: A schickt an B – A's Zustimmung steckt im Abschicken, B's Zustimmung
    ist der Accept-Klick. Damit sind beide Seiten einverstanden.
    """
    __tablename__ = "shift_swap_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_assignment_id: Mapped[int] = mapped_column(ForeignKey("shift_assignments.id", ondelete="CASCADE"))
    from_helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"))
    to_helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"), index=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending | accepted | declined | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    from_assignment: Mapped["ShiftAssignment"] = relationship()
    from_helper: Mapped["Helper"] = relationship(foreign_keys=[from_helper_id])
    to_helper: Mapped["Helper"] = relationship(foreign_keys=[to_helper_id])
