import json
import logging

import pytest

from campuslife_hub.redaction import REDACTED, Redactor, file_handler

CANARY = "canary-7f3a9c1e5b2d4f60a8e1"
NOTE = "canary note: the lecture said {x} and 'quoted' \"text\""


@pytest.fixture
def logged(tmp_path):
    """A logger writing to a log file through the redactor, and a way to read that file."""
    redactor = Redactor()
    handler = file_handler(tmp_path / "logs" / "hub.log", redactor)
    logger = logging.getLogger(f"campuslife_hub.test.{tmp_path.name}")
    logger.propagate = False
    logger.addHandler(handler)

    def text():
        handler.flush()
        return (tmp_path / "logs" / "hub.log").read_text(encoding="utf-8")

    yield redactor, logger, text
    logger.removeHandler(handler)
    handler.close()


def test_a_registered_secret_is_masked_in_messages_arguments_and_tracebacks_in_the_log_file(logged):
    redactor, logger, text = logged
    redactor.register(CANARY)

    logger.warning(f"token {CANARY} in the message")
    logger.warning("token %s in an argument", CANARY)
    logger.warning('access - "GET /login?code=%s HTTP/1.1" 303', CANARY)
    try:
        raise RuntimeError(f"failed with {CANARY}")
    except RuntimeError:
        logger.exception("request failed")

    written = text()
    assert CANARY not in written
    assert written.count(REDACTED) == 4
    assert "RuntimeError: failed with [redacted]" in written


def test_a_secret_registered_later_is_masked_from_then_on(logged):
    redactor, logger, text = logged

    logger.warning("before %s", "no secret here yet")
    redactor.register(CANARY)
    logger.warning("after %s", CANARY)

    assert CANARY not in text()


def test_a_secret_that_contains_another_is_masked_whole():
    redactor = Redactor()
    redactor.register(CANARY)
    redactor.register(CANARY + "-and-its-tail")

    assert redactor.redact(f"a {CANARY}-and-its-tail b") == f"a {REDACTED} b"


@pytest.mark.parametrize("secret", ["short", "", None, 12345678901234567890])
def test_a_short_or_non_text_secret_is_refused(secret):
    with pytest.raises(ValueError):
        Redactor().register(secret)


@pytest.mark.parametrize(
    "shown",
    [
        json.dumps({"id": "i1", "body": NOTE, "title": "Week 3"}),
        json.dumps({"id": "i1", "body": NOTE, "title": "Week 3"}, ensure_ascii=False, indent=2),
        repr({"id": "i1", "body": NOTE, "title": "Week 3"}),
        json.dumps({"op": "patch", "set": {"body": {"from_hash": "a" * 64, "to": NOTE}, "title": {"from": "Week 2", "to": "Week 3"}}}),
        repr({"op": "patch", "set": {"body": {"from_hash": None, "to": NOTE}, "title": {"from": "Week 2", "to": "Week 3"}}}),
    ],
    ids=["json", "indented json", "repr", "json patch", "repr patch"],
)
def test_the_text_of_a_note_is_masked_where_a_line_shows_it_and_other_fields_are_not(logged, shown):
    _, logger, text = logged

    logger.error("rejected %s", shown)

    written = text()
    assert "canary note" not in written
    assert REDACTED in written
    assert "Week 3" in written
