"""The extractor framework (#5, D16): what runs in an extractor's child process and what the Hub does around it.

Child side, imported by `python -I -m campuslife_hub.extract.child` and nothing that touches the database, the
network or a provider (tests/test_extract_boundary.py): child, protocol, limits, extracted, sniff, formats, safe_zip
and the extractors (text, fakes). Hub side: the extractor descriptors (extractors), running a child and judging what
it printed (running), building and checking the bodies (bodies), and the planner and job handlers (framework). Nothing
here imports the Hub side at package import, so the child loads only what it uses.
"""
