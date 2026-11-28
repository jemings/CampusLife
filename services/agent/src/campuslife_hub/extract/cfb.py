"""The one way into a Compound File Binary (OLE) container (the extractor issues' common decision): .doc, .xls,
.ppt, .hwp and the encrypted OOXML wrapper are opened here and nowhere else.

olefile 0.47 (BSD, pure Python) parses the container. What it raises on a damaged file (OSError, struct.error,
IndexError, its own OleFileError and the rest) ends the job as corrupt; MemoryError alone goes on, so the child exits
with EXIT_MEMORY and the input is taken for one that needs more memory than the limits allow. A stream is read whole,
so its declared size is checked against the caller's bound first. Only this module imports olefile
(tests/test_extract_boundary.py).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import olefile

from .protocol import Refused, Status


@contextmanager
def _corrupt_on_error(doing: str) -> Iterator[None]:
    try:
        yield
    except (MemoryError, Refused):
        raise
    except Exception as error:
        raise Refused(Status.CORRUPT, f"the compound file is damaged ({doing}): {type(error).__name__}: {error}") from None


class Compound:
    def __init__(self, ole: olefile.OleFileIO):
        self._ole = ole

    def root_entries(self) -> set[str]:
        """The names of the root storage's streams and storages."""
        with _corrupt_on_error("listing the root"):
            return {path[0] for path in self._ole.listdir(streams=True, storages=True)}

    def read(self, path: Sequence[str], *, max_bytes: int) -> bytes:
        """The stream at path (storage names, then the stream's); unsafe_input when it declares more than max_bytes."""
        with _corrupt_on_error(f"reading {'/'.join(path)}"):
            size = self._ole.get_size(list(path))
            if size > max_bytes:
                raise Refused(Status.UNSAFE_INPUT, f"stream {'/'.join(path)} declares {size} bytes, more than {max_bytes}")
            with self._ole.openstream(list(path)) as stream:
                return stream.read()


@contextmanager
def open_cfb(path: Path) -> Iterator[Compound]:
    with _corrupt_on_error("opening it"):
        ole = olefile.OleFileIO(str(path))
    try:
        yield Compound(ole)
    finally:
        ole.close()
