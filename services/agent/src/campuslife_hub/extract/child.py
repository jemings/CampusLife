"""The extractor child (S3): `python -I -m campuslife_hub.extract.child`, one request on stdin, one result on stdout.

It reads the input file and nothing else of the Hub: no database, no network, no provider (tests/test_extract_boundary.py
holds what it may import). A MemoryError exits with EXIT_MEMORY; any other exception propagates and exits 1 with no
result, which the Hub takes for a crash.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from . import sniff
from .extracted import Extracted
from .limits import Limits
from .protocol import ENTRY_PREFIX, EXIT_MEMORY, Op, Refused, Status


def _entry(name: str):
    module, _, function = name.partition(":")
    if not module.startswith(ENTRY_PREFIX):
        raise ValueError(f"an entry point lives in {ENTRY_PREFIX}, not {name!r}")
    return getattr(importlib.import_module(module), function)


def respond(request: dict) -> dict:
    limits = Limits.from_json(request["limits"])
    path = Path(request["path"])
    try:
        if Op(request["op"]) is Op.IDENTIFY:
            return {"status": Status.OK, "media_type": sniff.identify(path, limits)}
        extracted: Extracted = _entry(request["entry"])(path, request["params"], limits)
    except Refused as refused:
        return {"status": refused.status, "reason": refused.reason}
    status = Status.PARTIAL if extracted.partial else Status.OK
    return {"status": status, "blocks": extracted.blocks, "warnings": [{"code": code} for code in extracted.warnings]}


def main() -> int:
    request = json.loads(sys.stdin.buffer.read())
    try:
        output = json.dumps(respond(request), ensure_ascii=False).encode("utf-8")
    except MemoryError:
        return EXIT_MEMORY
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
