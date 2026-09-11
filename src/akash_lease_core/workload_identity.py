"""Canonical, opt-in workload identity for Akash deployment groups.

The wire format preserves the identity contract merged in ``just-akash``
commit ``175419a`` (#315). The public namespace changes from
``just_akash.workload_identity`` to ``akash_lease_core.workload_identity``.
The reader API additionally requires observed group IDs and typed completeness
evidence, and the SDL transformer binds encoded IDs to placement order. Those
checks close gaps in the original API without changing valid ``idv1`` names.

The ownership register is supplied explicitly by the caller. Legacy names and
malformed or mixed populations are held, never inferred to be CI. Parsing an
identity is attribution only; retirement still requires separate authority.

Release tokens use lowercase letters, digits, dots and underscores (no
hyphens), so field delimiters cannot be smuggled into an opaque release
identifier.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CLASSES",
    "IDENTITY_SCHEMA_VERSION",
    "MAX_UTC_UNIX_SECONDS",
    "GroupObservation",
    "Identity",
    "Population",
    "PopulationCompleteness",
    "classify_groups",
    "format_identity",
    "parse_identity",
    "transform_sdl",
]

CLASSES = frozenset({"ci-runner", "ci-payload", "staging-payload", "prod-payload"})
IDENTITY_SCHEMA_VERSION = 1
MAX_UTC_UNIX_SECONDS = 253402300799
_NUMBER = r"[1-9][0-9]{0,31}"
_RELEASE = r"[a-z0-9][a-z0-9._]{0,63}"
_REPOSITORY_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPOSITORY_NAME = r"[A-Za-z0-9_.-]{1,100}"


@dataclass(frozen=True)
class Identity:
    """One group identity and its shared workload lifecycle."""

    prefix: str
    owner: str
    workload_class: str
    group: int
    run: int | None = None
    attempt: int | None = None
    release: str | None = None
    expires: int | None = None
    version: int = IDENTITY_SCHEMA_VERSION

    @property
    def lifecycle(self) -> tuple:
        """Fields that must agree across every group in one deployment."""

        return (
            self.version,
            self.prefix,
            self.owner,
            self.workload_class,
            self.run,
            self.attempt,
            self.release,
            self.expires,
        )


@dataclass(frozen=True)
class GroupObservation:
    """One on-chain group name bound to its observed numeric group ID."""

    group: int
    name: object


class PopulationCompleteness(str, Enum):
    """Whether the caller proved it supplied the complete group population."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class Population:
    """Classification of every group name in one deployment."""

    identities: tuple[Identity, ...] = ()
    held: bool = True
    reason: str = "unclassified"
    observed_count: int = 0
    parsed_count: int = 0
    completeness: PopulationCompleteness = PopulationCompleteness.INCOMPLETE


def _register(register: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(register, Mapping) or not register:
        raise ValueError("an explicit nonempty ownership register is required")
    stems = {}
    repositories: set[str] = set()
    for prefix, owner in register.items():
        if not isinstance(prefix, str) or not re.fullmatch(
            r"[a-z0-9]+(?:[.-][a-z0-9]+)*[.-]?", prefix
        ):
            raise ValueError("invalid registered prefix")
        if not isinstance(owner, str) or owner.count("/") != 1:
            raise ValueError("invalid registered repository")
        repository_owner, repository_name = owner.split("/", 1)
        if (
            not re.fullmatch(_REPOSITORY_OWNER, repository_owner)
            or not re.fullmatch(_REPOSITORY_NAME, repository_name)
            or repository_name in {".", ".."}
            or not any(
                character.isascii() and character.isalnum() for character in repository_name
            )
        ):
            raise ValueError("invalid registered repository")
        canonical_repository = owner.casefold()
        if canonical_repository in repositories:
            raise ValueError("a repository may own only one registered prefix")
        repositories.add(canonical_repository)
        stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"
        if stem in stems:
            raise ValueError("ambiguous registered namespace delimiter")
        stems[stem] = prefix
    return stems


def _number(value: object) -> str:
    if type(value) is not int or not re.fullmatch(_NUMBER, str(value)):
        raise ValueError("identity numbers must be positive canonical integers")
    return str(value)


def _expiry(value: object) -> str:
    number = _number(value)
    if int(number) > MAX_UTC_UNIX_SECONDS:
        raise ValueError("expiry must be representable UTC Unix seconds")
    return number


def format_identity(identity: Identity, register: Mapping[str, str]) -> str:
    """Format one identity in the canonical ``idv1`` group-name form."""

    _register(register)
    if type(identity.version) is not int or identity.version != IDENTITY_SCHEMA_VERSION:
        raise ValueError("unknown identity schema version")
    if register.get(identity.prefix) != identity.owner:
        raise ValueError("identity does not match registered ownership")
    if identity.workload_class not in CLASSES:
        raise ValueError("unknown workload class")
    stem = identity.prefix if identity.prefix.endswith(("-", ".")) else identity.prefix + "-"
    name = f"{stem}idv1-class-{identity.workload_class}-g{_number(identity.group)}"
    if identity.workload_class.startswith("ci-"):
        if identity.release is not None or identity.expires is not None:
            raise ValueError("CI requires run/attempt and cannot carry release or expiry")
        return f"{name}-attempt-{_number(identity.attempt)}-run-{_number(identity.run)}-end"
    if identity.run is not None or identity.attempt is not None:
        raise ValueError("payload releases cannot carry CI lifecycle fields")
    if not isinstance(identity.release, str) or not re.fullmatch(_RELEASE, identity.release):
        raise ValueError("invalid release token")
    name += f"-release-{identity.release}"
    if identity.expires is not None:
        if identity.workload_class != "staging-payload":
            raise ValueError("only staging may carry explicit expiry")
        name += f"-expires-{_expiry(identity.expires)}"
    return name


def parse_identity(name: object, register: Mapping[str, str]) -> Identity | None:
    """Parse a canonical ``idv1`` name, returning ``None`` for an unknown shape."""

    stems = _register(register)
    if not isinstance(name, str):
        return None
    matches = [prefix for stem, prefix in stems.items() if name.startswith(stem + "idv1-class-")]
    if len(matches) != 1:
        return None
    prefix = matches[0]
    stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"
    tail = name[len(stem + "idv1-class-") :]
    match = re.fullmatch(
        rf"(?P<class>ci-runner|ci-payload)-g(?P<group>{_NUMBER})-attempt-(?P<attempt>{_NUMBER})-run-(?P<run>{_NUMBER})-end"
        rf"|(?P<payload>staging-payload|prod-payload)-g(?P<pgroup>{_NUMBER})-release-(?P<release>{_RELEASE})(?:-expires-(?P<expires>{_NUMBER}))?",
        tail,
    )
    if match is None:
        return None
    fields = match.groupdict()
    identity = Identity(
        prefix,
        register[prefix],
        fields["class"] or fields["payload"],
        int(fields["group"] or fields["pgroup"]),
        run=int(fields["run"]) if fields["run"] else None,
        attempt=int(fields["attempt"]) if fields["attempt"] else None,
        release=fields["release"],
        expires=int(fields["expires"]) if fields["expires"] else None,
        version=IDENTITY_SCHEMA_VERSION,
    )
    try:
        return identity if format_identity(identity, register) == name else None
    except ValueError:
        return None


def classify_groups(
    groups: object,
    register: Mapping[str, str],
    *,
    completeness: PopulationCompleteness,
) -> Population:
    """Classify a group population bound to observed IDs and completeness evidence."""

    _register(register)
    if not isinstance(completeness, PopulationCompleteness):
        raise ValueError("explicit PopulationCompleteness evidence is required")
    if not isinstance(groups, (list, tuple)) or not groups:
        return Population(reason="missing or unreadable groups")
    observed_count = len(groups)
    if any(not isinstance(group, GroupObservation) for group in groups):
        return Population(
            reason="missing or unreadable group observations",
            observed_count=observed_count,
            completeness=completeness,
        )
    observations = tuple(group for group in groups if isinstance(group, GroupObservation))
    parsed = tuple(parse_identity(group.name, register) for group in observations)
    identities = tuple(identity for identity in parsed if identity is not None)
    context = {
        "identities": identities,
        "observed_count": observed_count,
        "parsed_count": len(identities),
        "completeness": completeness,
    }
    if completeness is PopulationCompleteness.INCOMPLETE:
        return Population(reason="group population is incomplete or unverified", **context)
    if any(identity is None for identity in parsed):
        return Population(reason="legacy, unknown or malformed identity", **context)
    observed_ids: list[int] = []
    for observation in observations:
        try:
            observed_ids.append(int(_number(observation.group)))
        except ValueError:
            return Population(reason="invalid observed group identity", **context)
    if len(set(observed_ids)) != observed_count:
        return Population(reason="duplicate observed group identity", **context)
    if set(observed_ids) != set(range(1, observed_count + 1)):
        return Population(reason="incomplete canonical group identity population", **context)
    if len({identity.group for identity in identities}) != len(identities):
        return Population(reason="duplicate encoded group identity", **context)
    if any(
        identity.group != observed
        for identity, observed in zip(identities, observed_ids, strict=True)
    ):
        return Population(reason="encoded group disagrees with observed group identity", **context)
    if len({identity.lifecycle for identity in identities}) != 1:
        return Population(reason="conflicting lifecycle identities", **context)
    return Population(
        held=False,
        reason="classified; retirement requires separate authority",
        **context,
    )


def transform_sdl(
    document: dict, identities: Mapping[str, Identity], register: Mapping[str, str]
) -> dict:
    """Return a copy with every placement definition and service reference renamed.

    Accepts a parsed SDL, not a shell template. Caller must identify every placement
    explicitly; partial, unused, unknown or conflicting groups are rejected atomically.
    Service names, resource profiles, provider attributes and embedded scripts stay intact.
    """
    _register(register)
    try:
        placements = document["profiles"]["placement"]
        services = document["deployment"]
    except (KeyError, TypeError) as exc:
        raise ValueError("missing SDL placement structure") from exc
    if not isinstance(placements, dict) or not placements or set(placements) != set(identities):
        raise ValueError("identity mapping must cover exactly every placement")
    if not isinstance(services, dict) or not services:
        raise ValueError("missing SDL deployment references")
    expected_groups = {placement: group for group, placement in enumerate(placements, start=1)}
    if any(
        identity.group != expected_groups[placement] for placement, identity in identities.items()
    ):
        raise ValueError("identity group must match canonical SDL placement order")
    names = {old: format_identity(identity, register) for old, identity in identities.items()}
    observations = [
        GroupObservation(expected_groups[placement], names[placement]) for placement in placements
    ]
    if classify_groups(
        observations,
        register,
        completeness=PopulationCompleteness.COMPLETE,
    ).held:
        raise ValueError("SDL groups disagree or repeat an identity")
    referenced = set()
    for groups in services.values():
        if not isinstance(groups, dict) or not groups or not set(groups) <= set(placements):
            raise ValueError("unknown or unreadable SDL deployment placement reference")
        referenced.update(groups)
    if referenced != set(placements):
        raise ValueError("unused SDL placement definition")
    result = deepcopy(document)
    result["profiles"]["placement"] = {
        names[key]: value for key, value in result["profiles"]["placement"].items()
    }
    result["deployment"] = {
        service: {names[key]: value for key, value in groups.items()}
        for service, groups in result["deployment"].items()
    }
    return result
