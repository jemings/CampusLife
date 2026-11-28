from dataclasses import replace

import pytest

from campuslife_hub.extract.extractors import ALL, TEXT, Extractor
from campuslife_hub.extract.fakes import PagedLimits, fake
from campuslife_hub.extract.limits import DEFAULT_LIMITS, IDENTIFY_LIMITS, RENDITION_LIMITS, Limits
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
    assert build_registry(identify_limits=changed).pipeline("identify") != default_registry().pipeline("identify")
    assert Limits.from_json(changed.to_json()) == changed


def test_an_extractors_limits_change_its_pipeline_and_timeout_and_nothing_elses():
    """The common decision: a bound of one extractor retires that extractor's pipeline only, identify's included."""
    other = fake("echo", {"text/csv": {}})
    slower = replace(TEXT, limits=replace(DEFAULT_LIMITS, timeout_s=DEFAULT_LIMITS.timeout_s * 3))
    before, after = build_registry((TEXT, other)), build_registry((slower, other))

    assert before.pipeline("extract.text") != after.pipeline("extract.text")
    assert after.pipeline("extract.text").version == f"text-{TEXT.version}+L{slower.limits.hash8()}"
    assert after.timeout("extract.text").total_seconds() == DEFAULT_LIMITS.timeout_s * 3 + 60
    assert before.pipeline("identify") == after.pipeline("identify")
    assert before.pipeline("extract.fake_echo") == after.pipeline("extract.fake_echo")
    assert before.timeout("identify") == after.timeout("identify") == before.timeout("extract.fake_echo")


def test_identify_runs_under_its_own_limits():
    registry = build_registry(identify_limits=replace(IDENTIFY_LIMITS, timeout_s=7.0))

    assert registry.pipeline("identify").version.endswith(f"+L{replace(IDENTIFY_LIMITS, timeout_s=7.0).hash8()}")
    assert registry.timeout("identify").total_seconds() == 7.0 + 60
    assert registry.pipeline("extract.text") == default_registry().pipeline("extract.text")


def test_an_extractor_and_a_converter_start_from_their_own_default_limits():
    assert fake("echo", {"text/csv": {}}).limits is DEFAULT_LIMITS
    assert fake("pdf", {"text/csv": {}}, kind="render").limits is RENDITION_LIMITS


def test_the_default_table_keeps_the_hash_it_had_before_tables_had_types():
    """text keeps VERSION 1 through the Outline move, so its pipeline must not change with the table's JSON."""
    assert DEFAULT_LIMITS.hash8() == IDENTIFY_LIMITS.hash8() == "92874ddf"


def test_an_extractors_own_limits_type_keeps_its_bounds_through_json():
    limits = PagedLimits(max_pages=7, timeout_s=9.0)

    assert Limits.from_json(limits.to_json()) == limits
    assert limits.hash8() != PagedLimits(max_pages=8, timeout_s=9.0).hash8()


@pytest.mark.parametrize(
    "type_name",
    ["os:PathLike", "campuslife_hub.extract.extractors:Extractor", "campuslife_hub.extract.limits:nothing", "campuslife_hub.pipelines:Limits"],
)
def test_limits_from_json_take_only_a_limits_type_of_the_extractor_package(type_name):
    with pytest.raises(ValueError):
        Limits.from_json({**DEFAULT_LIMITS.to_json(), "type": type_name})
