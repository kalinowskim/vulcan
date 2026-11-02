"""Ponawianie z backoffem, blokada pliku i maskowanie sekretów w logach."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


def retry(
    fn: Callable[[], T],
    *,
    what: str,
    should_retry: Callable[[BaseException], bool],
    attempts: int = 3,
    delay: float = 5.0,
    backoff: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Wywołuje `fn`; przy błędzie, dla którego `should_retry` zwraca True, ponawia z rosnącym odstępem."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts or not should_retry(exc):
                raise
            wait = delay * backoff ** (attempt - 1)
            log.warning("%s: próba %d/%d nieudana (%s: %s), ponawiam za %.0f s",
                        what, attempt, attempts, type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "",
                        wait)  # fmt: skip
            sleep(wait)
    raise AssertionError("unreachable")


class FileLock:
    """Nieblokująca blokada pliku: drugi przebieg nie wystartuje, dopóki pierwszy trwa.

    Blokada systemowa (flock / msvcrt) zwalnia się sama, gdy proces padnie.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class SecretFilter(logging.Filter):
    """Zamienia znane sekrety (hasła z konfiguracji) na *** w każdym logowanym komunikacie."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        message = record.getMessage()
        redacted = self.redact(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True
