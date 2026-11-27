"""The bodies the Hub stores from a child's result (boundary): built by the Hub, checked against the contract.

The child's blocks are untrusted. The Hub keeps their order, NFC-normalizes every string in them, gives each ref
the Locator v and the Blob it was read from (a ref that names v, blob or capture itself is refused), derives each
block id from the Blob, the extractor and the ordinal, and checks the whole body with validate(). The size limits
of the limits table hold for the blocks and for the stored bytes, canonical_json(body).
"""

from __future__ import annotations

import hashlib
import unicodedata

from campuslife_contracts import canonical_json, validate

from .limits import Limits

LOCATOR_VERSION = 1
BODY_VERSION = 1
ANCHORS = ("v", "blob", "capture")


class Rejected(Exception):
    """The child's result breaks the contract; the job fails."""


class TooLarge(Exception):
    """The child's result breaks a size limit; the job fails with unsafe_input."""


def _nfc(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(each) for each in value]
    if isinstance(value, dict):
        return {_nfc(key): _nfc(each) for key, each in value.items()}
    return value


def block_id(blob: str, extractor_id: str, extractor_version: str, ordinal: int) -> str:
    return hashlib.sha256(canonical_json([blob, extractor_id, extractor_version, ordinal])).hexdigest()


def _ref(raw: object, blob: str, at: str) -> dict:
    if not isinstance(raw, dict):
        raise Rejected(f"{at} is not an object")
    named = sorted(set(raw) & set(ANCHORS))
    if named:
        raise Rejected(f"{at} names {', '.join(named)}, which the Hub gives")
    return {**raw, "v": LOCATOR_VERSION, "blob": blob}


def _block(raw: object, ordinal: int, blob: str, extractor: dict, limits: Limits) -> dict:
    at = f"block {ordinal}"
    if not isinstance(raw, dict):
        raise Rejected(f"{at} is not an object")
    if "id" in raw:
        raise Rejected(f"{at} names its id, which the Hub derives")
    text = raw.get("text")
    if isinstance(text, str) and len(text) > limits.max_block_text_chars:
        raise TooLarge(f"{at} holds {len(text)} characters, more than {limits.max_block_text_chars}")
    refs = raw.get("refs")
    if not isinstance(refs, list):
        raise Rejected(f"{at} has no refs list")
    block = {**raw, "refs": [_ref(ref, blob, f"{at} ref {index}") for index, ref in enumerate(refs)]}
    return {"id": block_id(blob, extractor["id"], extractor["version"], ordinal), **block}


def _checked(schema_id: str, body: dict, limits: Limits) -> bytes:
    violations = validate(schema_id, body)
    if violations:
        raise Rejected(f"the body breaks {schema_id}: {sorted(map(str, violations))[:5]}")
    stored = canonical_json(body)
    if len(stored) > limits.body_max_bytes:
        raise TooLarge(f"the body takes {len(stored)} bytes, more than {limits.body_max_bytes}")
    return stored


def extraction(blob: str, extractor: dict, result: dict, limits: Limits) -> bytes:
    """The stored bytes of the extraction body made from the child's ok or partial result."""
    blocks, warnings = result.get("blocks"), result.get("warnings", [])
    if not isinstance(blocks, list):
        raise Rejected("the result has no blocks list")
    if len(blocks) > limits.max_blocks:
        raise TooLarge(f"{len(blocks)} blocks, more than {limits.max_blocks}")
    total = sum(len(block["text"]) for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str))
    if total > limits.max_text_chars:
        raise TooLarge(f"the blocks hold {total} characters, more than {limits.max_text_chars}")
    body = {
        "v": BODY_VERSION,
        "blob": blob,
        "extractor": dict(extractor),
        "blocks": [_block(_nfc(raw), ordinal, blob, extractor, limits) for ordinal, raw in enumerate(blocks)],
    }
    if warnings:
        body["warnings"] = _nfc(warnings)
    return _checked("artifact/extraction", body, limits)


def identification(blob: str, identifier: dict, result: dict, limits: Limits) -> tuple[str, bytes]:
    """The media type the child found and the stored bytes of the identify body."""
    body = {"v": BODY_VERSION, "blob": blob, "identifier": dict(identifier), "media_type": result.get("media_type"), "blocks": []}
    return body["media_type"], _checked("artifact/identify", body, limits)
