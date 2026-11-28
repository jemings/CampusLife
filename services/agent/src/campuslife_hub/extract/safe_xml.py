"""The one way into XML (the extractor issues' common decision): OOXML and HWPX parts are parsed here and nowhere else.

Parsing is the standard library's expat, streamed a chunk at a time, and what it reports comes out as events: an
element's start with its attributes, its end, and its text. Names carry their namespace as "<uri> <local>", so a
Transitional and a Strict document differ only in the uri.

A document type declaration ends the parse with unsafe_input before expat reads anything inside it: no entity is
then declared, so none is expanded (billion laughs) and no external DTD or entity is loaded. expat breaching its
own amplification limit is unsafe_input too; any other parse error is corrupt. Only this module imports xml
(tests/test_extract_boundary.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO
from xml.parsers import expat

from .protocol import Refused, Status

CHUNK_BYTES = 64 * 1024
NAMESPACE_SEPARATOR = " "
AMPLIFICATION = expat.errors.codes[expat.errors.XML_ERROR_AMPLIFICATION_LIMIT_BREACH]


@dataclass(frozen=True)
class Start:
    name: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class End:
    name: str


@dataclass(frozen=True)
class Text:
    data: str


type Event = Start | End | Text


class _Doctype(Exception):
    pass


def refusal(error: expat.ExpatError) -> Refused:
    if error.code == AMPLIFICATION:
        return Refused(Status.UNSAFE_INPUT, f"the XML expands past expat's amplification limit: {error}")
    return Refused(Status.CORRUPT, f"the XML does not parse: {error}")


def _refuse_doctype(*_: object) -> None:
    raise _Doctype


class _Events:
    """What expat reported since the last take, with the text between two tags as one Text however many chunks
    and callbacks it came in."""

    def __init__(self) -> None:
        self._done: list[Event] = []
        self._text: list[str] = []

    def element(self, event: Start | End) -> None:
        self._close_text()
        self._done.append(event)

    def text(self, data: str) -> None:
        self._text.append(data)

    def _close_text(self) -> None:
        if self._text:
            self._done.append(Text("".join(self._text)))
            self._text.clear()

    def take(self, *, final: bool) -> list[Event]:
        """The events complete so far; text that may go on in the next chunk waits for it unless final."""
        if final:
            self._close_text()
        done, self._done = self._done, []
        return done


def events(source: BinaryIO) -> Iterator[Event]:
    """The events of the XML document source holds, read CHUNK_BYTES at a time. Refused with unsafe_input or
    corrupt as the module says; the events of a chunk come out only once the whole chunk parsed."""
    parser = expat.ParserCreate(namespace_separator=NAMESPACE_SEPARATOR)
    parser.buffer_text = True
    pending = _Events()
    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.StartElementHandler = lambda name, attributes: pending.element(Start(name, attributes))
    parser.EndElementHandler = lambda name: pending.element(End(name))
    parser.CharacterDataHandler = pending.text
    while True:
        chunk = source.read(CHUNK_BYTES)
        try:
            parser.Parse(chunk, not chunk)
        except _Doctype:
            raise Refused(Status.UNSAFE_INPUT, "the XML declares a document type") from None
        except expat.ExpatError as error:
            raise refusal(error) from None
        yield from pending.take(final=not chunk)
        if not chunk:
            return
