"""Log redaction (E19): a log line never holds a secret the Hub keeps or the text of a note.

Secrets are registered as they are made: the admin token, login codes and sessions here; device
tokens and pairing secrets by their owner (#19). They are masked wherever they appear in a
formatted line, tracebacks and access lines included. Text is the value of a contract x-text
column (an Item's body), masked where a line shows it as a JSON or Python member.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from pathlib import Path

from .user_data import TEXT_COLUMNS

REDACTED = "[redacted]"

MIN_SECRET_CHARS = 16
"""A short value would mask every place the same characters happen to appear."""

LOG_FILE_MAX_BYTES = 5 << 20
LOG_FILE_BACKUPS = 5

TEXT_FIELDS = frozenset().union(*TEXT_COLUMNS.values())

_QUOTED = r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
_TEXT_MEMBER = re.compile(
    rf"""(["'](?:{"|".join(map(re.escape, sorted(TEXT_FIELDS)))})["']\s*:\s*)(?:{_QUOTED}|\{{(?:{_QUOTED}|[^{{}}"'])*\}})"""
)


class Redactor:
    def __init__(self) -> None:
        self._secrets: frozenset[str] = frozenset()
        self._lock = threading.Lock()

    def register(self, secret: str) -> str:
        """Mask secret in every line formatted from now on; the secret itself, for chaining."""
        if not isinstance(secret, str) or len(secret) < MIN_SECRET_CHARS:
            raise ValueError(f"a registered secret has at least {MIN_SECRET_CHARS} characters")
        with self._lock:
            self._secrets |= {secret}
        return secret

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return _TEXT_MEMBER.sub(lambda match: f'{match[1]}"{REDACTED}"', text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor, fmt: str | None = None) -> None:
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


def file_handler(path: Path, redactor: Redactor) -> logging.Handler:
    """A rotating log file whose every line passes through redactor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
    handler.setFormatter(RedactingFormatter(redactor, "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    return handler
