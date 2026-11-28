import io
import time

import pytest

from campuslife_hub.extract import safe_xml
from campuslife_hub.extract.protocol import Refused, Status
from campuslife_hub.extract.safe_xml import End, Start, Text, events

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCUMENT = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:p><w:t xml:space="preserve">1장 트리</w:t></w:p></w:document>'.encode()

LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
 <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
 <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
 <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<lolz>&lol9;</lolz>"""


def refusal(data):
    with pytest.raises(Refused) as refused:
        list(events(io.BytesIO(data)))
    return refused.value.status


def test_elements_and_text_come_in_document_order_with_namespaced_names():
    assert list(events(io.BytesIO(DOCUMENT))) == [
        Start(f"{W} document", {}),
        Start(f"{W} p", {}),
        Start(f"{W} t", {"http://www.w3.org/XML/1998/namespace space": "preserve"}),
        Text("1장 트리"),
        End(f"{W} t"),
        End(f"{W} p"),
        End(f"{W} document"),
    ]


def test_a_document_is_read_in_chunks_and_text_across_them_comes_whole(monkeypatch):
    monkeypatch.setattr(safe_xml, "CHUNK_BYTES", 7)
    data = f"<a>{'가' * 40}</a>".encode()

    assert list(events(io.BytesIO(data))) == [Start("a", {}), Text("가" * 40), End("a")]


@pytest.mark.parametrize(
    "data",
    [
        LAUGHS,
        b'<?xml version="1.0"?><!DOCTYPE a SYSTEM "http://example.invalid/a.dtd"><a/>',
        b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>',
        b"<!DOCTYPE a><a/>",
    ],
    ids=["billion_laughs", "external_dtd", "external_entity", "bare_doctype"],
)
def test_a_doctype_is_unsafe_before_anything_is_expanded(data):
    started = time.monotonic()

    assert refusal(data) is Status.UNSAFE_INPUT
    assert time.monotonic() - started < 1


def test_nothing_after_a_refused_doctype_reaches_the_reader():
    seen = []
    with pytest.raises(Refused):
        for event in events(io.BytesIO(b"<!DOCTYPE a [<!ENTITY x 'y'>]><a>&x;</a>")):
            seen.append(event)

    assert seen == []


@pytest.mark.parametrize(
    "data",
    [b"<a><b></a>", b"<a>&undefined;</a>", b"", b"<a/><b/>", b"\xff\xfe<\x00"],
    ids=["mismatched_tag", "undefined_entity", "empty", "two_roots", "broken_encoding"],
)
def test_a_parse_error_is_corrupt(data):
    assert refusal(data) is Status.CORRUPT


def test_expat_breaching_its_amplification_limit_is_unsafe():
    """No entity is declared without a DOCTYPE, which is refused first, so expat's own limit is the second guard."""
    from xml.parsers import expat

    error = expat.ExpatError("limit on input amplification factor (from DTD and entities) breached")
    error.code = expat.errors.codes[expat.errors.XML_ERROR_AMPLIFICATION_LIMIT_BREACH]

    assert safe_xml.refusal(error).status is Status.UNSAFE_INPUT
    error.code = expat.errors.codes[expat.errors.XML_ERROR_TAG_MISMATCH]
    assert safe_xml.refusal(error).status is Status.CORRUPT
