"""Canonical workload identity is opt-in and fail-closed across every group."""

from copy import deepcopy
from dataclasses import replace

import pytest

from akash_lease_core.workload_identity import (
    CLASSES,
    IDENTITY_SCHEMA_VERSION,
    MAX_UTC_UNIX_SECONDS,
    GroupObservation,
    Identity,
    Population,
    PopulationCompleteness,
    classify_groups,
    format_identity,
    parse_identity,
    transform_sdl,
)

REGISTER = {
    "borduas": "Borduas-Holdings/blazing",
    "dfci-infra-": "Borduas-Holdings/Blazing-Back",
    "just-akash-runner.": "Digital-Frontier-LDA/just-akash",
}


def identity(prefix="borduas", workload_class="ci-runner", group=1):
    if workload_class.startswith("ci-"):
        return Identity(prefix, REGISTER[prefix], workload_class, group, run=12345, attempt=2)
    return Identity(prefix, REGISTER[prefix], workload_class, group, release="abc123.v1_2")


def classify(names, *, groups=None, completeness=PopulationCompleteness.COMPLETE):
    observed = range(1, len(names) + 1) if groups is None else groups
    observations = [
        GroupObservation(group, name) for group, name in zip(observed, names, strict=True)
    ]
    return classify_groups(observations, REGISTER, completeness=completeness)


@pytest.mark.parametrize("prefix", list(REGISTER))
@pytest.mark.parametrize("workload_class", sorted(CLASSES))
def test_registered_namespace_and_class_round_trip(prefix, workload_class):
    original = identity(prefix, workload_class)
    name = format_identity(original, REGISTER)
    stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"

    assert name.startswith(stem + "idv1-class-")
    assert parse_identity(name, REGISTER) == original
    assert classify([name], groups=[original.group]).held is False


def test_staging_expiry_is_explicit_and_preserved():
    original = replace(identity(workload_class="staging-payload"), expires=1800000000)

    assert parse_identity(format_identity(original, REGISTER), REGISTER) == original
    with pytest.raises(ValueError, match="only staging"):
        format_identity(replace(original, workload_class="prod-payload"), REGISTER)


def test_staging_expiry_must_be_representable_utc_seconds():
    maximum = replace(identity(workload_class="staging-payload"), expires=MAX_UTC_UNIX_SECONDS)

    assert parse_identity(format_identity(maximum, REGISTER), REGISTER) == maximum
    with pytest.raises(ValueError, match="representable UTC"):
        format_identity(replace(maximum, expires=MAX_UTC_UNIX_SECONDS + 1), REGISTER)
    with pytest.raises(ValueError, match="representable UTC"):
        format_identity(replace(maximum, expires=1800000000000), REGISTER)


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_formatter_rejects_unknown_or_noncanonical_schema_versions(version):
    with pytest.raises(ValueError, match="schema version"):
        format_identity(replace(identity(), version=version), REGISTER)


def test_parser_rejects_an_unknown_schema_version():
    canonical = format_identity(identity(), REGISTER)

    assert IDENTITY_SCHEMA_VERSION == 1
    assert parse_identity(canonical.replace("-idv1-", "-idv2-"), REGISTER) is None


@pytest.mark.parametrize(
    "change",
    [
        {"run": None},
        {"attempt": 0},
        {"group": True},
        {"owner": "someone/else"},
        {"prefix": "unknown"},
        {"release": "hidden"},
        {"expires": 1800000000},
        {"workload_class": "unknown"},
        {"run": -1},
        {"group": "1"},
    ],
)
def test_formatter_rejects_missing_or_conflicting_fields(change):
    with pytest.raises(ValueError):
        format_identity(replace(identity(), **change), REGISTER)


@pytest.mark.parametrize(
    "release", ["", "release-with-hyphens", "abc-expires-123", "CAPS", "../bad", "a" * 65]
)
def test_release_tokens_are_not_silently_normalized(release):
    with pytest.raises(ValueError, match="release token"):
        format_identity(
            replace(identity(workload_class="prod-payload"), release=release), REGISTER
        )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "borduas-runner-run-1-end",
        "just-akash-runner.run-1-end",
        "borduas-idv2-class-ci-runner-g1-attempt-2-run-12345-end",
        "borduas-idv1-class-ci-runner-g1-attempt-2-run-12345-end-run-9-end",
        "borduas-idv1-class-ci-runner-g1-attempt-02-run-12345-end",
        "borduas-idv1-class-prod-payload-g1-release-abc-expires-1800000000",
        "borduas-idv1-class-staging-payload-g1-release-abc-release-def",
        "borduas-idv1-class-unknown-g1-release-abc",
        "stranger-idv1-class-ci-runner-g1-attempt-2-run-12345-end",
    ],
)
def test_unreadable_legacy_or_invalid_names_are_held(bad):
    assert parse_identity(bad, REGISTER) is None
    assert classify([format_identity(identity(), REGISTER), bad]).held


@pytest.mark.parametrize(
    "change",
    [
        {"attempt": 3},
        {"run": 88},
        {"workload_class": "ci-payload"},
        {"prefix": "dfci-infra-", "owner": REGISTER["dfci-infra-"]},
    ],
)
def test_group_lifecycle_disagreement_is_held(change):
    first = identity()
    second = replace(first, group=2, **change)

    assert classify([format_identity(x, REGISTER) for x in (first, second)]).held


def test_all_groups_are_retained_and_duplicate_groups_are_held():
    names = [format_identity(identity(group=n), REGISTER) for n in (1, 2)]
    population = classify(names)

    assert not population.held
    assert [item.group for item in population.identities] == [1, 2]
    assert classify(names + names[:1]).held


def test_incomplete_population_is_held_and_retains_diagnostics():
    names = [format_identity(identity(group=n), REGISTER) for n in (1, 2)]
    population = classify(names, completeness=PopulationCompleteness.INCOMPLETE)

    assert population.held
    assert population.reason == "group population is incomplete or unverified"
    assert population.completeness is PopulationCompleteness.INCOMPLETE
    assert population.observed_count == 2
    assert population.parsed_count == 2
    assert population.identities == (identity(group=1), identity(group=2))


@pytest.mark.parametrize("completeness", [True, False, "complete", None])
def test_completeness_requires_typed_evidence(completeness):
    with pytest.raises(ValueError, match="PopulationCompleteness"):
        classify_groups([], REGISTER, completeness=completeness)


def test_a_truncated_population_cannot_classify_from_group_two_alone():
    name = format_identity(identity(group=2), REGISTER)
    population = classify([name], groups=[2])

    assert population.held
    assert population.reason == "incomplete canonical group identity population"


def test_noncontiguous_observed_group_population_is_held():
    names = [format_identity(identity(group=n), REGISTER) for n in (1, 3)]
    population = classify(names, groups=[1, 3])

    assert population.held
    assert population.reason == "incomplete canonical group identity population"


def test_duplicate_observed_group_identity_is_held():
    names = [format_identity(identity(group=n), REGISTER) for n in (1, 2)]
    population = classify(names, groups=[1, 1])

    assert population.held
    assert population.reason == "duplicate observed group identity"


@pytest.mark.parametrize("observed", [0, -1, True, "1", None])
def test_invalid_observed_group_identity_is_held(observed):
    name = format_identity(identity(), REGISTER)
    population = classify([name], groups=[observed])

    assert population.held
    assert population.reason == "invalid observed group identity"


def test_encoded_group_must_equal_the_observed_group():
    names = [format_identity(identity(group=n), REGISTER) for n in (2, 1)]
    population = classify(names, groups=[1, 2])

    assert population.held
    assert population.reason == "encoded group disagrees with observed group identity"


def test_malformed_sibling_is_preserved_as_a_measured_held_population():
    valid = format_identity(identity(), REGISTER)
    population = classify([valid, "legacy"])

    assert population.held
    assert population.observed_count == 2
    assert population.parsed_count == 1
    assert population.identities == (identity(),)


@pytest.mark.parametrize("names", [None, {}, set(), "name", [], ()])
def test_missing_or_non_sequence_populations_are_held(names):
    result = classify_groups(names, REGISTER, completeness=PopulationCompleteness.COMPLETE)

    assert result == Population(reason="missing or unreadable groups")


@pytest.mark.parametrize(
    "register",
    [
        {},
        {"borduas": "a/b", "borduas-": "c/d"},
        {"bad/": "a/b"},
        {"Good": "a/b"},
        {"good": "missing-slash"},
        {1: "a/b"},
        {"good": 1},
        {"good": "../.."},
        {"good": "-owner/repo"},
        {"good": "owner-/repo"},
        {"good": "owner/.."},
        {"good": "owner/---"},
        {"one": "Org/Repo", "two": "org/repo"},
        [],
        None,
    ],
)
def test_invalid_register_is_a_configuration_error(register):
    with pytest.raises(ValueError):
        parse_identity("legacy", register)


@pytest.mark.parametrize(
    "owner",
    [
        "a/b",
        "Digital-Frontier-LDA/just-akash",
        "Borduas-Holdings/.github",
        "org/repo_name.v2",
    ],
)
def test_valid_repository_identifiers_are_accepted(owner):
    register = {"repo": owner}
    value = Identity("repo", owner, "ci-runner", 1, run=1, attempt=1)

    assert parse_identity(format_identity(value, register), register) == value


@pytest.mark.parametrize("field,value", [("release", "different"), ("expires", 1800000001)])
def test_release_or_expiry_disagreement_is_held(field, value):
    first = replace(identity(workload_class="staging-payload"), expires=1800000000)
    second = replace(first, group=2, **{field: value})

    assert classify([format_identity(item, REGISTER) for item in (first, second)]).held


def test_parser_accepts_only_canonical_numbers_and_exact_names():
    canonical = format_identity(identity(), REGISTER)

    assert parse_identity(canonical.replace("-g1-", "-g01-"), REGISTER) is None
    assert parse_identity(canonical.replace("-run-12345-", "-run-0-"), REGISTER) is None
    assert parse_identity(canonical + "-suffix", REGISTER) is None
    assert parse_identity(canonical.upper(), REGISTER) is None


def test_identity_numbers_have_a_32_digit_ceiling():
    maximum = replace(identity(), group=int("9" * 32), run=int("9" * 32), attempt=int("9" * 32))

    assert parse_identity(format_identity(maximum, REGISTER), REGISTER) == maximum
    with pytest.raises(ValueError, match="positive canonical"):
        format_identity(replace(maximum, run=int("1" * 33)), REGISTER)


def test_classification_reason_distinguishes_each_hold_mode():
    valid = format_identity(identity(), REGISTER)
    conflict = format_identity(replace(identity(), group=2, attempt=3), REGISTER)

    assert classify([]).reason == "missing or unreadable groups"
    assert classify([valid, "legacy"]).reason == ("legacy, unknown or malformed identity")
    assert classify([valid, conflict]).reason == "conflicting lifecycle identities"
    assert classify([valid, valid]).reason == "duplicate encoded group identity"
    assert classify([valid]).reason == ("classified; retirement requires separate authority")


def sdl():
    return {
        "version": "2.0",
        "services": {
            "api": {"command": ["sh", "-c", "echo api"]},
            "worker": {"env": ["QUEUE=work"]},
        },
        "profiles": {
            "compute": {"api": {"resources": {"cpu": {"units": "1"}}}},
            "placement": {
                "west": {"attributes": {"region": "west"}},
                "east": {"attributes": {"region": "east"}},
            },
        },
        "deployment": {
            "api": {"west": {"profile": "api", "count": 1}},
            "worker": {"east": {"profile": "api", "count": 2}},
        },
    }


def mappings():
    return {"west": identity(group=1), "east": identity(group=2)}


def test_transform_rewrites_every_placement_and_reference_without_mutation():
    document = sdl()
    before = deepcopy(document)
    mapping = mappings()
    changed = transform_sdl(document, mapping, REGISTER)
    names = {old: format_identity(item, REGISTER) for old, item in mapping.items()}

    assert document == before
    assert changed is not document
    assert set(changed["profiles"]["placement"]) == set(names.values())
    assert (
        changed["profiles"]["placement"][names["west"]] == before["profiles"]["placement"]["west"]
    )
    assert (
        changed["profiles"]["placement"][names["east"]] == before["profiles"]["placement"]["east"]
    )
    assert set(changed["deployment"]["api"]) == {names["west"]}
    assert set(changed["deployment"]["worker"]) == {names["east"]}
    assert changed["services"] == before["services"]
    assert changed["profiles"]["compute"] == before["profiles"]["compute"]


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"west": identity(group=1)},
        {**mappings(), "unknown": identity(group=3)},
    ],
)
def test_transform_requires_exact_placement_coverage(mapping):
    with pytest.raises(ValueError, match="exactly every placement"):
        transform_sdl(sdl(), mapping, REGISTER)


def test_transform_rejects_unused_placement_definitions():
    document = sdl()
    del document["deployment"]["worker"]

    with pytest.raises(ValueError, match="unused SDL placement"):
        transform_sdl(document, mappings(), REGISTER)


def test_transform_rejects_unknown_deployment_references():
    document = sdl()
    document["deployment"]["api"]["missing"] = {"profile": "api", "count": 1}

    with pytest.raises(ValueError, match="unknown or unreadable"):
        transform_sdl(document, mappings(), REGISTER)


@pytest.mark.parametrize("groups", [None, [], "west", {}, {"west", "east"}])
def test_transform_rejects_unreadable_or_empty_reference_groups(groups):
    document = sdl()
    document["deployment"]["api"] = groups

    with pytest.raises(ValueError, match="unknown or unreadable"):
        transform_sdl(document, mappings(), REGISTER)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"profiles": None},
        {"profiles": {}},
        {"profiles": {"placement": {}}, "deployment": {}},
        {"profiles": {"placement": []}, "deployment": {"api": {}}},
    ],
)
def test_transform_rejects_missing_or_empty_sdl_structure(document):
    with pytest.raises(ValueError):
        transform_sdl(document, {}, REGISTER)


@pytest.mark.parametrize(
    "mapping",
    [
        {"west": identity(group=1), "east": identity(group=1)},
        {"west": identity(group=1), "east": replace(identity(group=2), run=999)},
        {
            "west": identity(group=1),
            "east": identity(prefix="dfci-infra-", workload_class="ci-runner", group=2),
        },
    ],
)
def test_transform_rejects_duplicate_or_conflicting_group_lifecycles(mapping):
    with pytest.raises(ValueError):
        transform_sdl(sdl(), mapping, REGISTER)


def test_transform_binds_encoded_groups_to_canonical_placement_order():
    reversed_groups = {"west": identity(group=2), "east": identity(group=1)}

    with pytest.raises(ValueError, match="canonical SDL placement order"):
        transform_sdl(sdl(), reversed_groups, REGISTER)


def test_transform_preserves_order_and_values_while_renaming_only_keys():
    document = sdl()
    changed = transform_sdl(document, mappings(), REGISTER)
    placement_names = list(changed["profiles"]["placement"])

    assert placement_names == [
        format_identity(identity(group=1), REGISTER),
        format_identity(identity(group=2), REGISTER),
    ]
    assert list(changed["deployment"]) == ["api", "worker"]
