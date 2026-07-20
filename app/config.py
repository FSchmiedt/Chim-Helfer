"""Konfiguration – liest aus .env / Umgebungsvariablen."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # DB
    DATABASE_URL: str = "sqlite:///./chimaera.db"

    # Admin
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "change-me"

    # Session
    SECRET_KEY: str = "change-me-to-something-long-and-random"

    # Festival
    FESTIVAL_NAME: str = "Chimaera Festival"

    # Anmeldungen offen? Auf false stellen, um das öffentliche Anmeldeformular
    # zu deaktivieren (Banner statt Formular). Login, /me, Schichttausch und
    # Admin bleiben weiter erreichbar.
    REGISTRATION_OPEN: bool = True
    # Optionale eigene Nachricht im "Anmeldungen geschlossen"-Banner.
    # Leer = Default-Text.
    REGISTRATION_CLOSED_MESSAGE: str = ""

    # Selbst-Eintragen in Schichten ab wann offen? Default false, weil der
    # Schichtplan oft noch nicht fertig ist, wenn die App live geht.
    # Admin sieht /schichten immer (für Vorschau).
    SHIFT_SIGNUP_OPEN: bool = False

    # Optional: Zeitgesteuerte Freischaltung. ISO-8601-Zeitpunkt mit Zeitzone,
    # z.B. "2026-07-10T14:00:00+02:00" (Berlin Sommerzeit). Ab diesem Moment
    # ist der Self-Signup automatisch offen, ohne dass jemand einen Schalter
    # umlegen muss. SHIFT_SIGNUP_OPEN=true überschreibt das (schaltet sofort frei).
    SHIFT_SIGNUP_OPEN_AT: str = ""

    # Optional: Komma-getrennte Liste von Email-Adressen, die den Schichtplan
    # schon VOR der Freischaltung sehen und testen dürfen (z.B. Orga-Team).
    # Beispiel: "test@example.org, orga@example.org"
    SHIFT_SIGNUP_PREVIEW_EMAILS: str = ""

    # Bereiche, die vom Tausch-Board ausgeschlossen sind (Komma-getrennt).
    # Diese Schichten kann man nicht aufs Board stellen; Tausch macht der Admin
    # manuell. Default: Bar.
    SWAP_EXCLUDED_AREAS: str = "Bar"

    # Anforderungen (nur Anzeige)
    MIN_SHIFTS: int = 2
    MIN_DAYS: int = 2
    BENEFITS_MIN_SHIFTS: int = 3
    BENEFITS_MIN_DAYS: int = 3

    # SMTP (optional)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_ADDRESS: str = "helfer@example.org"
    SMTP_FROM_NAME: str = "Chimaera Helfer-Team"
    SMTP_USE_TLS: bool = True
    # Sekunden bis SMTP aufgibt. Der Versand laeuft im Hintergrund, blockiert
    # also keine Seite mehr - ein haengender Server soll den Worker trotzdem
    # nicht ewig belegen.
    SMTP_TIMEOUT: int = 20

    # Nur für Dev: Passwort-Reset-Links auf der Seite anzeigen, wenn SMTP aus ist.
    # In Produktion immer False lassen.
    DEBUG_SHOW_RESET_LINK: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.SMTP_USER and self.SMTP_PASSWORD)

    @property
    def shift_signup_effective_open(self) -> bool:
        """Ist der Self-Signup gerade offen?

        True, wenn entweder der manuelle Schalter an ist ODER die zeitgesteuerte
        Freischaltung (SHIFT_SIGNUP_OPEN_AT) erreicht/überschritten ist.
        """
        if self.SHIFT_SIGNUP_OPEN:
            return True
        opens_at = self._parse_signup_open_at()
        if opens_at is None:
            return False
        from datetime import datetime, timezone
        return datetime.now(timezone.utc) >= opens_at

    def _parse_signup_open_at(self):
        """Parst SHIFT_SIGNUP_OPEN_AT zu einem tz-aware datetime, oder None."""
        raw = (self.SHIFT_SIGNUP_OPEN_AT or "").strip()
        if not raw:
            return None
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        # Falls ohne Zeitzone angegeben, als UTC interpretieren (defensiv).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @property
    def shift_signup_preview_emails(self) -> set[str]:
        """Set der Preview-Email-Adressen (lowercase, getrimmt)."""
        raw = self.SHIFT_SIGNUP_PREVIEW_EMAILS or ""
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    @property
    def swap_excluded_areas(self) -> set[str]:
        """Set der Bereichsnamen (lowercase), die nicht getauscht werden dürfen."""
        raw = self.SWAP_EXCLUDED_AREAS or ""
        return {a.strip().lower() for a in raw.split(",") if a.strip()}


settings = Settings()
