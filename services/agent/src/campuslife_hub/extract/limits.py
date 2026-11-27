"""The limits table (S7): every bound an identify, extract or render job runs under, in one frozen value.

A job carries the table in its request, the child keeps to it, and the Hub checks what comes back against it. Its
hash is part of the pipeline version, so a change of any bound makes new jobs rather than reusing results made under
other bounds. Every breach ends the job with outcome unsafe_input (FILE-INGESTION §9).

Where the defaults come from: local measurements of the real child on synthetic inputs (Linux, CPython 3.12, 2026-11-28;
generated Korean and English Markdown, generated ZIPs), not on users' files:
- the Hub builds and validates a body off the event loop, at about 0.78 ms a block whatever the file's size (NFC, block
  ids, the contract validator, canonical JSON; Markdown read by the text extractor): 1.6 s for 2,000 blocks, 3.8 s
  for 5,000, 7.7 s for 10,000, 15.8 s for 20,000, 38 s for 50,000. Nearly all of it is jsonschema checking each block
  and its Locator. It runs in a worker thread, so it stalls the event loop at no block count; it holds the one job
  runner, inside the handler timeout (timeout_s and HANDLER_SLACK). max_blocks at 20,000, about 16 s, leaves a 300-page
  textbook of some 3,000 blocks whole with room; 50,000, about 38 s, would leave the timeout little room. An extractor
  stops at max_blocks, partial;
- the text extractor with no bounds took 0.82 s and 318 MiB of memory for 64 MiB of text and printed 84 MiB, a 106 MiB
  body of 168,252 blocks; 128 MiB took 1.7 s and 617 MiB. Under these defaults any such file stops at max_blocks,
  partial: 0.2 s, at most 141 MiB, 10 MiB printed, a 12.6 MiB body. A file of few, long blocks stops at
  max_text_chars instead: all-Korean text, 3 bytes a character, prints at most about 24 MiB, so stdout_max_bytes and
  body_max_bytes hold with room;
- identify read a 10,000-member ZIP in 0.05 s and 26 MiB;
- deflate packed 64 MiB of zeros, a zip bomb, 1,029 times; a 1 MiB Markdown file of one paragraph repeated 140 times;
  one paragraph alone 3.8 times. zip_max_ratio parts them;
- timeout_s, mem_bytes, input_max_bytes and rendition_max_bytes are not measured here: they leave the format extractors and the converter to come (#6,
  #7, #13, O4) the room a lecture PDF or deck of a few hundred MiB needs, and those issues measure them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

MiB = 1 << 20


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 120.0
    """Wall time of one child run."""
    mem_bytes: int = 1024 * MiB
    """Memory of the child: its address space on POSIX (RLIMIT_AS), its committed memory on Windows (Job Object)."""
    stdout_max_bytes: int = 64 * MiB
    """What the child may print; past it the child is killed."""
    input_max_bytes: int = 256 * MiB
    """The Blob an extract or render job reads whole; a larger one is not handed to its child. identify reads only
    the head of a Blob, so it takes one of any size."""
    max_blocks: int = 20_000
    """Blocks of one body: the Hub builds and validates them off the event loop in about 16 s."""
    max_block_text_chars: int = 200_000
    max_text_chars: int = 8_000_000
    """The text of all blocks together; an extractor stops there, partial."""
    body_max_bytes: int = 64 * MiB
    """The body the Hub stores, as canonical JSON."""
    rendition_max_bytes: int = 512 * MiB
    """The PDF a converter writes; a larger one is not stored."""
    zip_max_members: int = 10_000
    zip_max_total_bytes: int = 1024 * MiB
    """What the members of one archive declare uncompressed, together."""
    zip_max_member_bytes: int = 256 * MiB
    zip_max_ratio: int = 200
    """Uncompressed to compressed size of one member: past it the member is taken for a zip bomb."""
    zip_max_depth: int = 2
    """Archives inside archives: 1 opens only the outer one."""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Limits:
        return cls(**data)

    def hash8(self) -> str:
        """The first 8 hex digits of the table's SHA-256: the L part of a pipeline version."""
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()[:8]


DEFAULT_LIMITS = Limits()
