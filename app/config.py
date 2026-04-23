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

    # Nur für Dev: Passwort-Reset-Links auf der Seite anzeigen, wenn SMTP aus ist.
    # In Produktion immer False lassen.
    DEBUG_SHOW_RESET_LINK: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.SMTP_USER and self.SMTP_PASSWORD)


settings = Settings()
