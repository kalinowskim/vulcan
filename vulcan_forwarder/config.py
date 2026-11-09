"""Konfiguracja z pliku .env / zmiennych środowiskowych."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    pass


# Nazwa konta z jednym kontem skonfigurowanym po staremu (VULCAN_LOGIN / VULCAN_PASSWORD / MAIL_TO).
DEFAULT_ACCOUNT = "default"
ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Account(BaseModel):
    """Konto eduVULCAN i adresy, na które idą jego wiadomości."""

    name: str
    login: str
    password: SecretStr
    mail_to: str  # kilka adresów po przecinku/średniku

    @property
    def recipients(self) -> str:
        return ", ".join(split_list(self.mail_to))

    def state_dir(self, settings: Settings) -> Path:
        # Konto domyślne trzyma pliki tam, gdzie wersja jednokontowa (zgodność wstecz).
        if self.name == DEFAULT_ACCOUNT:
            return settings.state_dir
        return settings.state_dir / "accounts" / self.name


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

    # Kilka kont: VULCAN_ACCOUNTS=mateusz,aga + VULCAN_<NAZWA>_LOGIN/_PASSWORD/_MAIL_TO (patrz Settings.load).
    vulcan_accounts: str = ""
    # Jedno konto (zapis zgodny z poprzednią wersją), gdy VULCAN_ACCOUNTS jest puste.
    vulcan_login: str = ""
    vulcan_password: SecretStr = SecretStr("")

    poll_interval_minutes: int = 5
    # Co ile minut sprawdzać Vulcana niezależnie od powiadomień (0 = tylko na powiadomienie).
    full_check_interval_minutes: int = 60
    notification_sender_filter: str = ""

    # Dziennik ucznia: podsumowanie tygodnia (oceny + nowe sprawdziany/zadania) w sobotę o WEEKLY_DIGEST_HOUR
    # i codzienne (nd–czw) sprawdzenie kalendarza o DAILY_CHECK_HOUR — pilne wpisy idą od razu.
    diary_enabled: bool = True
    weekly_digest_hour: int = Field(default=9, ge=0, le=23)
    daily_check_hour: int = Field(default=17, ge=0, le=23)
    alert_email_to: str = ""

    state_dir: Path = Path("state")
    debug_dir: Path = Path("debug")
    db_path: Path = Path("state/forwarder.db")

    # Wypełniane przez Settings.load() z VULCAN_ACCOUNTS; nie czytane bezpośrednio ze środowiska.
    account_list: list[Account] = Field(default_factory=list, exclude=True)

    @classmethod
    def load(cls, env_file: str | Path | None = ".env", environ: Mapping[str, str] | None = None) -> Settings:
        """Ustawienia z .env i środowiska, razem z kontami z VULCAN_ACCOUNTS."""
        environ = os.environ if environ is None else environ
        settings = cls(_env_file=env_file)
        names = [n.lower() for n in split_list(settings.vulcan_accounts)]
        if not names:
            return settings

        file_values = dotenv_values(env_file) if env_file and Path(env_file).exists() else {}
        values = {k.upper(): v for k, v in file_values.items() if v is not None}
        values.update({k.upper(): v for k, v in environ.items()})  # środowisko wygrywa, jak w pydantic

        accounts = []
        for name in names:
            if not ACCOUNT_NAME_RE.match(name):
                raise ConfigError(f"Nieprawidłowa nazwa konta w VULCAN_ACCOUNTS: {name!r} (dozwolone: a-z, 0-9, _ i -)")
            prefix = f"VULCAN_{name.upper().replace('-', '_')}_"
            login = values.get(prefix + "LOGIN", "")
            password = values.get(prefix + "PASSWORD", "")
            missing = [prefix + f for f, v in (("LOGIN", login), ("PASSWORD", password)) if not v]
            if missing:
                raise ConfigError(f"Brak wymaganych ustawień w .env: {', '.join(missing)}")
            mail_to = values.get(prefix + "MAIL_TO", "") or settings.mail_to
            if not split_list(mail_to):
                raise ConfigError(f"Brak {prefix}MAIL_TO (ani ogólnego MAIL_TO) dla konta {name}")
            accounts.append(Account(name=name, login=login, password=SecretStr(password), mail_to=mail_to))
        if len(set(names)) != len(names):
            raise ConfigError("Powtórzona nazwa konta w VULCAN_ACCOUNTS")
        settings.account_list = accounts
        return settings

    @property
    def accounts(self) -> list[Account]:
        """Skonfigurowane konta Vulcana (pierwsze przejmuje dane z wersji jednokontowej)."""
        if self.account_list:
            return self.account_list
        if not self.vulcan_login:
            return []
        return [Account(name=DEFAULT_ACCOUNT, login=self.vulcan_login, password=self.vulcan_password,
                        mail_to=self.mail_to)]  # fmt: skip

    def require_accounts(self) -> list[Account]:
        if not self.accounts:
            raise ConfigError("Brak kont Vulcana: ustaw VULCAN_ACCOUNTS (+ VULCAN_<NAZWA>_*) albo VULCAN_LOGIN/VULCAN_PASSWORD")
        return self.accounts

    @property
    def secrets(self) -> list[str]:
        """Wszystkie hasła z konfiguracji — do maskowania w logach."""
        values = [self.imap_password, self.smtp_password, self.vulcan_password, *(a.password for a in self.accounts)]
        return [s.get_secret_value() for s in values]

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
        """ALERT_EMAIL_TO (kilka adresów jak w MAIL_TO), domyślnie MAIL_TO, a bez niego — adresy wszystkich kont."""
        explicit = ", ".join(split_list(self.alert_email_to)) or self.mail_recipient
        if explicit:
            return explicit
        seen: dict[str, None] = {}
        for account in self.accounts:
            seen.update(dict.fromkeys(split_list(account.mail_to)))
        return ", ".join(seen)

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
SMTP_FIELDS = ("smtp_host", "mail_from")
