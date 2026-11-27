"""What a Hub sends an extractor child on stdin and what the child prints on stdout (S3, S4).

The request is JSON: op (identify, extract or render), the input path, the limits table, and for extract and render
the entry point of the extractor or converter and its params, and for render the out path. The child prints one JSON result and exits 0, or exits without one: EXIT_MEMORY when
it ran out of memory, anything else when it crashed. A result has a status; ok and partial carry the media type
(identify) or the blocks and warnings (extract); a render's ok carries nothing more, its PDF is the out file. Blocks name places by Locator members other than v and blob, which
the Hub adds.
"""

from __future__ import annotations

import enum

EXIT_MEMORY = 86
"""The child's exit code after a MemoryError: the input needs more memory than the limits allow."""

ENTRY_PREFIX = "campuslife_hub.extract."
"""An extractor's entry point lives in this package, the only code a child imports."""


class Op(enum.StrEnum):
    IDENTIFY = "identify"
    EXTRACT = "extract"
    RENDER = "render"
    """The request also names out, the file in the work directory the converter writes the PDF to."""


class Status(enum.StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    """Only part of the input was read; what was read is returned, with a warning saying why."""
    CORRUPT = "corrupt"
    PASSWORD_REQUIRED = "password_required"
    UNSUPPORTED = "unsupported"
    NEEDS_CONVERSION = "needs_conversion"
    UNSAFE_INPUT = "unsafe_input"
    """The input breaks a limit or has a dangerous structure (an archive member outside the archive, a zip bomb)."""


class Refused(Exception):
    """An extractor or the identifier ends with a status other than ok or partial."""

    def __init__(self, status: Status, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason
