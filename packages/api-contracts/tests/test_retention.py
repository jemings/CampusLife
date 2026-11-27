"""How long a client keeps the Mutations the Hub acknowledged (D24 decision 5)."""

import campuslife_contracts
from campuslife_contracts import SENT_RETENTION_DAYS


def test_a_client_keeps_an_acknowledged_mutation_for_60_days():
    """Shorter than the Hub's 90 days of results, so a kept Mutation's first result is still on the Hub (D21)."""
    assert SENT_RETENTION_DAYS == 60
    assert "SENT_RETENTION_DAYS" in campuslife_contracts.__all__
