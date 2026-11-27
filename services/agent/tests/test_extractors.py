import pytest

from campuslife_hub.extract.extractors import ALL, TEXT, Extractor
from campuslife_hub.extract.fakes import fake
from campuslife_hub.extract.limits import DEFAULT_LIMITS, Limits
from campuslife_hub.pipelines import build_registry, default_registry


@pytest.mark.parametrize(
    "arguments",
    [
        ("Text", "1", "campuslife_hub.extract.text:extract"),
        ("text", "1-2", "campuslife_hub.extract.text:extract"),
        ("text", "1+L00", "campuslife_hub.extract.text:extract"),
        ("text", ".1", "campuslife_hub.extract.text:extract"),
        ("text", "1", "os:system"),
        ("text", "1", "campuslife_hub.extract.text"),
        ("text", "1", "campuslife_hub.db:extract"),
    ],
)
def test_a_descriptor_refuses_what_would_break_a_job_type_a_pipeline_version_or_the_child(arguments):
    with pytest.raises(ValueError):
        Extractor(*arguments)


def test_the_default_registry_serves_identify_and_every_extractor_and_plans_files():
    registry = default_registry()

    assert registry.job_types == {"capture.ingest", "identify", *(extractor.job_type for extractor in ALL)}
    assert [name for name, _ in registry.planners] == ["files"]
    assert registry.pipeline("extract.text").version == f"text-{TEXT.version}+L{DEFAULT_LIMITS.hash8()}"
    assert registry.timeout("extract.text").total_seconds() == DEFAULT_LIMITS.timeout_s + 60


def test_an_extractor_registers_by_its_descriptor_alone():
    registry = build_registry((*ALL, fake("echo", {"text/csv": {}})))

    assert "extract.fake_echo" in registry.job_types


def test_two_extractors_cannot_share_a_job_type():
    with pytest.raises(ValueError):
        build_registry((TEXT, TEXT))


def test_any_change_of_a_limit_changes_the_pipeline_versions():
    changed = Limits(zip_max_depth=DEFAULT_LIMITS.zip_max_depth + 1)

    assert changed.hash8() != DEFAULT_LIMITS.hash8()
    assert build_registry(limits=changed).pipeline("identify") != default_registry().pipeline("identify")
    assert Limits.from_json(changed.to_json()) == changed
