"""Konfiguracja z pliku .env / zmiennych środowiskowych."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: SecretStr = SecretStr("")
    imap_folder: str = "INBOX"
    imap_processed_folder: str = "Processed"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    mail_from: str = ""
    mail_to: str = ""

    vulcan_login: str = ""
    vulcan_password: SecretStr = SecretStr("")

    poll_interval_minutes: int = 5
    # Co ile minut sprawdzać Vulcana niezależnie od powiadomień (0 = tylko na powiadomienie).
    full_check_interval_minutes: int = 60
    notification_sender_filter: str = ""
    alert_email_to: str = ""

    state_dir: Path = Path("state")
    debug_dir: Path = Path("debug")
    db_path: Path = Path("state/forwarder.db")

    @property
    def sender_filters(self) -> list[str]:
        """NOTIFICATION_SENDER_FILTER może zawierać kilka adresów/domen rozdzielonych przecinkiem."""
        return split_list(self.notification_sender_filter)

    @property
    def mail_recipient(self) -> str:
        """MAIL_TO jako nagłówek To; kilka adresów można rozdzielić przecinkiem lub średnikiem."""
        return ", ".join(split_list(self.mail_to))

    @property
    def alert_recipient(self) -> str:
        """ALERT_EMAIL_TO (kilka adresów jak w MAIL_TO), domyślnie MAIL_TO."""
        return ", ".join(split_list(self.alert_email_to)) or self.mail_recipient

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    def require(self, *fields: str) -> None:
        """Rzuca ConfigError, jeśli któreś z wymaganych pól jest puste."""
        missing = []
        for name in fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not value:
                missing.append(name.upper())
        if missing:
            raise ConfigError(f"Brak wymaganych ustawień w .env: {', '.join(missing)}")


def split_list(value: str) -> list[str]:
    """„a@x.pl, b@y.pl; c@z.pl” → ['a@x.pl', 'b@y.pl', 'c@z.pl']."""
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


IMAP_FIELDS = ("imap_host", "imap_user", "imap_password")
SMTP_FIELDS = ("smtp_host", "mail_from", "mail_to")
VULCAN_FIELDS = ("vulcan_login", "vulcan_password")
