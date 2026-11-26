"""from_hash for text columns (E11) and its golden vectors.

fixtures/text_hash/<case>.json: {"text": "...", "hash": "<64 lowercase hex>"}.
A column that has no text sends from_hash null; null is never hashed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .jsonio import NotStrictJson, read_strict

TEXT_HASH_DIR = "text_hash"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


def text_hash(text: str) -> str:
    """sha256 (lowercase hex) of the text's UTF-8 bytes after NFC and CRLF/CR to LF."""
    if not isinstance(text, str):
        raise TypeError(f"text_hash takes a str, not {type(text).__name__}")
    canonical = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TextHashVector:
    name: str
    path: Path


def discover_text_hash_vectors(root: Path) -> list[TextHashVector]:
    base = Path(root) / "fixtures" / TEXT_HASH_DIR
    return [TextHashVector(path.stem, path) for path in sorted(base.glob("*.json"))]


def text_hash_vector_problems(vector: TextHashVector) -> list[str]:
    """Empty when text_hash gives the vector's hash."""
    try:
        data = read_strict(vector.path)
    except NotStrictJson as error:
        return [f"not strict JSON: {error}"]
    if not isinstance(data, dict):
        return ["a vector is a JSON object"]
    if set(data) != {"text", "hash"}:
        return ["members must be exactly hash, text"]
    if not isinstance(data["text"], str):
        return ["text must be a string; a column without text sends from_hash null and is not hashed"]
    if not isinstance(data["hash"], str) or not HEX_SHA256.fullmatch(data["hash"]):
        return ["hash must be 64 lowercase hex digits"]
    actual = text_hash(data["text"])
    if actual != data["hash"]:
        return [f"text_hash gives {actual}, the vector expects {data['hash']}"]
    return []


def text_hash_layout_problems(root: Path) -> dict[Path, str]:
    base = Path(root) / "fixtures" / TEXT_HASH_DIR
    problems: dict[Path, str] = {}
    for path in base.iterdir() if base.is_dir() else ():
        if path.is_dir():
            problems[path] = f"vectors sit directly under {TEXT_HASH_DIR}"
        elif path.suffix != ".json":
            problems[path] = "not a .json file"
    return problems
