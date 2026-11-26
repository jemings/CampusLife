"""The local UI's import form, a multipart/form-data body written into staging files as it streams in.

Each file goes to a file of its own in staging/ with the staging suffix, so one a crash leaves
behind is removed with the other staging leftovers at the next start. Small text fields are kept
in memory, bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from .blobs import STAGING_SUFFIX

MAX_FIELD_BYTES = 1024
MAX_FIELDS = 16
MAX_FILES = 1000


class FormRefused(Exception):
    pass


@dataclass(frozen=True)
class StagedFile:
    path: Path
    name: str


@dataclass
class StagedForm:
    fields: dict[str, str] = field(default_factory=dict)
    files: list[StagedFile] = field(default_factory=list)

    def discard(self) -> None:
        """One try per file; a file that stays is a staging leftover the next start removes."""
        for staged in self.files:
            with contextlib.suppress(OSError):
                staged.path.unlink()


def _text(raw: bytes | bytearray) -> str:
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise FormRefused("a form name or field is not UTF-8") from None


class _Parts:
    """python-multipart callbacks; file data is queued for the event loop to write off its thread."""

    def __init__(self, form: StagedForm, staging_dir: Path) -> None:
        self.form = form
        self.staging_dir = staging_dir
        self.opened: list[BinaryIO] = []
        self.writes: list[tuple[BinaryIO, bytes]] = []
        self._header_name = self._header_value = self._disposition = b""
        self._name = ""
        self._file: BinaryIO | None = None
        self._value: bytearray | None = None

    def on_part_begin(self) -> None:
        self._disposition, self._file, self._value = b"", None, None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = self._header_value
        self._header_name = self._header_value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        if b"name" not in options:
            raise FormRefused("a form part has no name")
        self._name = _text(options[b"name"])
        if b"filename" not in options:
            if len(self.form.fields) >= MAX_FIELDS:
                raise FormRefused(f"more than {MAX_FIELDS} fields")
            self._value = bytearray()
            return
        name = _text(options[b"filename"])
        if not name:
            return
        if len(self.form.files) >= MAX_FILES:
            raise FormRefused(f"more than {MAX_FILES} files in one import")
        path = self.staging_dir / f"{uuid.uuid4()}{STAGING_SUFFIX}"
        self._file = open(path, "xb")
        self.opened.append(self._file)
        self.form.files.append(StagedFile(path, name))

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._file is not None:
            self.writes.append((self._file, bytes(data[start:end])))
        elif self._value is not None:
            if len(self._value) + end - start > MAX_FIELD_BYTES:
                raise FormRefused(f"a field is longer than {MAX_FIELD_BYTES} bytes")
            self._value += data[start:end]

    def on_part_end(self) -> None:
        if self._value is not None:
            self.form.fields[self._name] = _text(self._value)


def _write(writes: list[tuple[BinaryIO, bytes]]) -> None:
    for file, data in writes:
        file.write(data)


async def stage_form(content_type: str, body: AsyncIterator[bytes], staging_dir: Path) -> StagedForm:
    """The form's fields, and its files in staging_dir under the names the browser sent; FormRefused for a body that
    is not such a form. The caller discards the files once it is done with them."""
    kind, options = parse_options_header(content_type)
    if kind != b"multipart/form-data" or b"boundary" not in options:
        raise FormRefused("the import form is sent as multipart/form-data")
    staging_dir.mkdir(parents=True, exist_ok=True)
    form = StagedForm()
    parts = _Parts(form, staging_dir)
    callbacks = {
        "on_part_begin": parts.on_part_begin,
        "on_header_field": parts.on_header_field,
        "on_header_value": parts.on_header_value,
        "on_header_end": parts.on_header_end,
        "on_headers_finished": parts.on_headers_finished,
        "on_part_data": parts.on_part_data,
        "on_part_end": parts.on_part_end,
    }
    try:
        parser = MultipartParser(options[b"boundary"], callbacks)
        async for chunk in body:
            parser.write(chunk)
            if parts.writes:
                writes, parts.writes = parts.writes, []
                await asyncio.to_thread(_write, writes)
        parser.finalize()
    except BaseException as error:
        for file in parts.opened:
            file.close()
        form.discard()
        if isinstance(error, FormParserError):
            raise FormRefused("the import form is not valid multipart/form-data") from None
        raise
    for file in parts.opened:
        file.close()
    return form
