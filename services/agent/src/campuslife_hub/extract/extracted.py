"""What an extractor returns in the child: blocks in reading order, warnings, and whether it read all of it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Extracted:
    blocks: list[dict] = field(default_factory=list)
    """Each {block_type, text?, payload?, refs}: refs are Locators without v and blob, which the Hub adds."""
    warnings: list[str] = field(default_factory=list)
    partial: bool = False
    """Only part of the input was read; a warning says why."""

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)
