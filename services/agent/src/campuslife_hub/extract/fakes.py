"""Fake extractors and converters for tests (decision on identify's input, S12): registered only by tests, through
pipelines.build_registry, to prove that an extractor plugs in by its descriptor alone and that what one does wrong
stays in its child. Each reads its params; none is in extractors.ALL.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from .extracted import Extracted
from .extractors import Extractor
from .limits import Limits
from .protocol import Refused, Status
from .safe_zip import SafeZip, open_zip

ENTRY = "campuslife_hub.extract.fakes"


def echo(path: Path, params: Mapping, limits: Limits) -> Extracted:
    """The blocks and warnings in params, partial when params say so."""
    return Extracted(list(params.get("blocks", [])), list(params.get("warnings", [])), bool(params.get("partial", False)))


def show_params(path: Path, params: Mapping, limits: Limits) -> Extracted:
    """One block holding the params the child was handed, as JSON."""
    return Extracted([{"block_type": "paragraph", "text": json.dumps(dict(params), sort_keys=True), "refs": [{"kind": "document"}]}])


def crash(path: Path, params: Mapping, limits: Limits) -> Extracted:
    raise RuntimeError("the fake extractor crashed")


def hang(path: Path, params: Mapping, limits: Limits) -> Extracted:
    while True:
        time.sleep(1)


def hog(path: Path, params: Mapping, limits: Limits) -> Extracted:
    held = []
    while True:
        held.append(bytearray(64 << 20))
        held[-1][-1] = 1


def flood(path: Path, params: Mapping, limits: Limits) -> Extracted:
    sys.stdout.buffer.write(b"x" * (limits.stdout_max_bytes + 1))
    return Extracted()


def garbage(path: Path, params: Mapping, limits: Limits) -> Extracted:
    sys.stdout.buffer.write(b"not json ")
    return Extracted()


def exit_silently(path: Path, params: Mapping, limits: Limits) -> Extracted:
    os._exit(0)


def lone_surrogate(path: Path, params: Mapping, limits: Limits) -> Extracted:
    """A result whose text is a lone surrogate, written as JSON escapes the child itself would not print, then exit."""
    sys.stdout.buffer.write(b'{"status": "ok", "blocks": [{"block_type": "paragraph", "text": "\\ud800", "refs": [{"kind": "document"}]}]}')
    sys.stdout.buffer.flush()
    os._exit(0)


def refuse(path: Path, params: Mapping, limits: Limits) -> Extracted:
    raise Refused(Status(params["status"]), "the fake extractor refused")


def modules(path: Path, params: Mapping, limits: Limits) -> Extracted:
    """A block listing the modules the child has loaded, one with its working directory and env, and one with what
    that directory holds."""
    loaded = "\n".join(sorted(sys.modules))
    where = f"{os.getcwd()}\n" + "\n".join(sorted(os.environ))
    held = "\n".join(["held:", *sorted(os.listdir())])
    return Extracted([{"block_type": "paragraph", "text": text, "refs": [{"kind": "document"}]} for text in (loaded, where, held)])


def plant(path: Path, params: Mapping, limits: Limits) -> Extracted:
    """One block, after leaving in its working directory a directory named as the Hub once named the body it wrote."""
    os.mkdir("body.json")
    return Extracted([{"block_type": "paragraph", "text": "planted", "refs": [{"kind": "document"}]}])


def _members(archive: SafeZip, prefix: str) -> list[str]:
    names = []
    for member in archive.members:
        names.append(prefix + member.name)
        if member.name.endswith(".zip"):
            with archive.open_nested(member.name) as nested:
                names += _members(nested, f"{prefix}{member.name}/")
    return names


def zip_walk(path: Path, params: Mapping, limits: Limits) -> Extracted:
    """A block per member of the archive and of the archives in it, read through safe_zip as #13 would."""
    with open_zip(path, limits) as archive:
        names = _members(archive, "")
    return Extracted([{"block_type": "paragraph", "text": name, "refs": [{"kind": "document"}]} for name in names])


def pdf(path: Path, params: Mapping, limits: Limits, out: Path) -> None:
    """A converter (kind render) writing a small PDF made of the input's bytes, as LibreOffice (O4) would one of a deck."""
    out.write_bytes(b"%PDF-1.7\n%" + str(params.get("mark", "")).encode() + b"\n" + path.read_bytes() + b"\n%%EOF\n")


def not_pdf(path: Path, params: Mapping, limits: Limits, out: Path) -> None:
    out.write_bytes(b"<html>not a PDF</html>")


def no_pdf(path: Path, params: Mapping, limits: Limits, out: Path) -> None:
    """A converter that says it converted and wrote nothing."""


def pdf_link(path: Path, params: Mapping, limits: Limits, out: Path) -> None:
    """A converter that leaves a link to a PDF where the PDF should be."""
    elsewhere = out.with_name("elsewhere.pdf")
    pdf(path, params, limits, elsewhere)
    out.symlink_to(elsewhere)


def refuse_render(path: Path, params: Mapping, limits: Limits, out: Path) -> None:
    raise Refused(Status(params["status"]), "the fake converter refused")


def fake(
    name: str, accepts: Mapping[str, Mapping[str, object]], *, version: str = "1", extractor_id: str | None = None, kind: str = "extract",
) -> Extractor:
    """A descriptor running the fake function name for the media types in accepts."""
    return Extractor(extractor_id or f"fake_{name}", version, f"{ENTRY}:{name}", accepts, kind)
