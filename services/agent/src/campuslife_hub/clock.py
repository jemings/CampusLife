"""Wall-clock times in the contract's instant form (E13)."""

from __future__ import annotations

from datetime import UTC, datetime


def instant(moment: datetime) -> str:
    """YYYY-MM-DDTHH:MM:SS.sssZ in UTC."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def now_instant() -> str:
    return instant(datetime.now(UTC))
