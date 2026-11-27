"""Extractor descriptors (decision 11): what an extractor reads and how the framework runs it.

An extractor, or a converter (kind render), is a function in this package, run in the child by its entry point; the framework owns the child,
the limits, the mapping of its result and the recording. Registering one is listing its descriptor in ALL (or, for
a test, passing it to pipelines.build_registry); nothing else changes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from . import formats, text
from .formats import Reader
from .protocol import ENTRY_PREFIX

ID = re.compile(r"[a-z][a-z0-9_]*")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._]*")
"""No - or +: the pipeline version is <id>-<version>+L<limits hash>."""
KINDS = ("extract", "render")


@dataclass(frozen=True)
class Extractor:
    id: str
    version: str
    entry: str
    """module:function in campuslife_hub.extract. An extract function takes (path, params, limits) and returns
    Extracted; a render function takes (path, params, limits, out) and writes a PDF to out."""
    accepts: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    """The params it takes for each media type it reads, as refine returns them."""
    kind: str = "extract"
    """extract makes an extraction Artifact, render a rendition Artifact holding the PDF (S5, S12)."""

    def __post_init__(self) -> None:
        if not ID.fullmatch(self.id) or len(self.id) > 64:
            raise ValueError(f"an extractor id matches {ID.pattern}, not {self.id!r}")
        if not VERSION.fullmatch(self.version) or len(self.version) > 64:
            raise ValueError(f"an extractor version matches {VERSION.pattern}, not {self.version!r}")
        module, colon, function = self.entry.partition(":")
        if not (module.startswith(ENTRY_PREFIX) and colon and function.isidentifier()):
            raise ValueError(f"an entry point is {ENTRY_PREFIX}<module>:<function>, not {self.entry!r}")
        if self.kind not in KINDS:
            raise ValueError(f"an extractor's kind is one of {', '.join(KINDS)}, not {self.kind!r}")
        object.__setattr__(self, "accepts", MappingProxyType({media: MappingProxyType(dict(params)) for media, params in self.accepts.items()}))

    @property
    def job_type(self) -> str:
        return f"{self.kind}.{self.id}"

    def reader(self) -> Reader:
        return Reader(self.job_type, self.accepts)


TEXT = Extractor(
    "text", text.VERSION, "campuslife_hub.extract.text:extract",
    {formats.TEXT: {"syntax": "plain"}, "text/markdown": {"syntax": "markdown"}},
)

ALL: tuple[Extractor, ...] = (TEXT,)
