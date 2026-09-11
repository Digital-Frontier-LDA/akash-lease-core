"""Sans-I/O creation admission and containment-budget contract.

This module proposes capacity reservations; a proposal is never create
authority. It does not read the chain, authenticate evidence, persist state, or
provide atomicity by itself. A broker must persist each proposal with
compare-and-swap and authenticate the resulting receipt. A redeemable permit
then requires a second CAS into ``SUBMITTING`` before the core issues network
submission authority.

Each reservation conservatively occupies three independent populations: one
active-deployment slot, one unresolved-outcome slot, and the uACT exposure in
typed backend-policy evidence. Typed lifecycle evidence releases them
independently.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum

from .chain_identity import is_canonical_akash_owner
from .create_journal import (
    BackendIdentity,
    BoundDeployment,
    CreateOutcome,
    CreateOutcomeKind,
    ExecutionClosure,
    NonCommitEvidence,
    PreparedCreate,
    RejectionEvidence,
    SettlementEvidence,
    SettlementState,
    UncertaintyEvidence,
    canonical_journal_digest,
)
from .workload_identity import CLASSES, MAX_UTC_UNIX_SECONDS

ADMISSION_SCHEMA_VERSION = 1
CREATE_PERMIT_AUDIENCE = "akash-create-submission"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class CensusStatus(str, Enum):
    VERIFIED = "verified"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class ReservationState(str, Enum):
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    CREATE_OUTCOME_UNKNOWN = "create_outcome_unknown"
    NON_COMMIT_PROVEN = "non_commit_proven"
    COMMITTED = "committed"
    EXECUTION_CLOSED = "execution_closed"
    SETTLED = "settled"


class AdmissionDisposition(str, Enum):
    PROPOSED = "proposed"
    RECONCILE = "reconcile"
    HOLD = "hold"


class PermitRevocationStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class AdmissionReason(str, Enum):
    PROPOSED = "proposed"
    RECONCILE_EXISTING = "reconcile_existing"
    MISSING_REQUIRED_BUDGET = "missing_required_budget"
    STALE_REVISION = "stale_revision"
    OPERATION_CONFLICT = "operation_conflict"
    JOURNAL_UNHEALTHY = "journal_unhealthy"
    RECOVERY_READER_UNHEALTHY = "recovery_reader_unhealthy"
    CHAIN_CENSUS_UNHEALTHY = "chain_census_unhealthy"
    ACCOUNTING_CENSUS_UNHEALTHY = "accounting_census_unhealthy"
    CENSUS_NOT_CURRENT = "census_not_current"
    EXPOSURE_EVIDENCE_NOT_CURRENT = "exposure_evidence_not_current"
    ACTIVE_DEPLOYMENT_LIMIT = "active_deployment_limit"
    UNRESOLVED_OUTCOME_LIMIT = "unresolved_outcome_limit"
    FINANCIAL_EXPOSURE_LIMIT = "financial_exposure_limit"
    EXPOSURE_POLICY_MISMATCH = "exposure_policy_mismatch"
    RESERVATION_CENSUS_MISMATCH = "reservation_census_mismatch"


class ReservationTransitionError(ValueError):
    """Raised when reservation evidence would skip or rewrite lifecycle state."""


class PersistenceConfirmationError(ValueError):
    """Raised when post-CAS evidence disagrees with the proposed state."""


def _version(value: object) -> None:
    if type(value) is not int or value != ADMISSION_SCHEMA_VERSION:
        raise ValueError("unsupported creation-admission schema version")


def _nonempty_ascii(value: object, name: str, *, maximum: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or not value.isascii()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must be nonempty canonical ASCII")


def _digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _time(value: object, name: str) -> None:
    if type(value) is not int or not 0 < value <= MAX_UTC_UNIX_SECONDS:
        raise ValueError(f"{name} must be canonical UTC Unix seconds")


def _count(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def canonical_reservation_population_digest(operation_ids: tuple[str, ...]) -> str:
    """Digest the complete, sorted operation population excluded from a census."""

    if type(operation_ids) is not tuple:
        raise ValueError("reservation operation population must be a tuple")
    for operation_id in operation_ids:
        _nonempty_ascii(operation_id, "operation_id", maximum=128)
    if operation_ids != tuple(sorted(set(operation_ids))):
        raise ValueError("reservation operation population must be sorted and unique")
    return hashlib.sha256(
        json.dumps(operation_ids, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


EMPTY_RESERVATION_POPULATION_DIGEST = canonical_reservation_population_digest(())


@dataclass(frozen=True, order=True)
class AdmissionScope:
    """The exact isolation boundary to which one set of limits applies."""

    chain_id: str
    owner: str
    producer_issuer: str
    repository_owner_id: str
    repository_id: str
    repository: str
    workload_class: str
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _nonempty_ascii(self.chain_id, "chain_id")
        if not is_canonical_akash_owner(self.owner):
            raise ValueError("owner must be a canonical Akash account")
        _nonempty_ascii(self.producer_issuer, "producer_issuer")
        for name in ("repository_owner_id", "repository_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
                raise ValueError(f"{name} must be a positive canonical integer string")
        _nonempty_ascii(self.repository, "repository")
        if self.repository.count("/") != 1:
            raise ValueError("repository must be an owner/name identity")
        if self.workload_class not in CLASSES:
            raise ValueError("unknown workload class")


@dataclass(frozen=True, order=True)
class OwnerBudgetScope:
    """Owner-wide aggregate shared by every producer leaf using the account."""

    chain_id: str
    owner: str
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _nonempty_ascii(self.chain_id, "chain_id")
        if not is_canonical_akash_owner(self.owner):
            raise ValueError("owner must be a canonical Akash account")


@dataclass(frozen=True)
class ContainmentLimits:
    max_active_deployments: int
    max_unresolved_create_outcomes: int
    max_financial_exposure_uact: int
    exposure_policy_revision: str
    admission_policy_revision: str
    policy_reference: str
    valid_until: int
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _count(self.max_active_deployments, "max_active_deployments")
        _count(self.max_unresolved_create_outcomes, "max_unresolved_create_outcomes")
        _count(self.max_financial_exposure_uact, "max_financial_exposure_uact")
        if (
            not isinstance(self.exposure_policy_revision, str)
            or _REVISION.fullmatch(self.exposure_policy_revision) is None
        ):
            raise ValueError("exposure_policy_revision must be a lowercase 40-character revision")
        if (
            not isinstance(self.admission_policy_revision, str)
            or _REVISION.fullmatch(self.admission_policy_revision) is None
        ):
            raise ValueError("admission_policy_revision must be a lowercase 40-character revision")
        _nonempty_ascii(self.policy_reference, "policy_reference")
        _time(self.valid_until, "valid_until")


@dataclass(frozen=True)
class ContainmentCensus:
    """A complete external census excluding reservations carried in this state."""

    active_deployments: int
    unresolved_create_outcomes: int
    financial_exposure_uact: int
    excluded_reservation_count: int
    excluded_reservation_population_digest: str
    journal_status: CensusStatus
    recovery_reader_status: CensusStatus
    chain_status: CensusStatus
    accounting_status: CensusStatus
    observed_at: int
    valid_until: int
    evidence_digest: str
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in (
            "active_deployments",
            "unresolved_create_outcomes",
            "financial_exposure_uact",
            "excluded_reservation_count",
        ):
            _count(getattr(self, name), name)
        _digest(
            self.excluded_reservation_population_digest,
            "excluded_reservation_population_digest",
        )
        for name in (
            "journal_status",
            "recovery_reader_status",
            "chain_status",
            "accounting_status",
        ):
            if not isinstance(getattr(self, name), CensusStatus):
                raise ValueError(f"{name} must be typed")
        _time(self.observed_at, "observed_at")
        _time(self.valid_until, "valid_until")
        if self.valid_until < self.observed_at:
            raise ValueError("census validity cannot predate observation")
        _digest(self.evidence_digest, "evidence_digest")


@dataclass(frozen=True)
class ScopeBudget:
    scope: OwnerBudgetScope | AdmissionScope
    limits: ContainmentLimits
    census: ContainmentCensus
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.scope, (OwnerBudgetScope, AdmissionScope)):
            raise ValueError("budget scope must be typed")
        if not isinstance(self.limits, ContainmentLimits):
            raise ValueError("limits must be typed")
        if not isinstance(self.census, ContainmentCensus):
            raise ValueError("census must be typed")


@dataclass(frozen=True)
class CreateExposureEvidence:
    """Trusted adapter evidence for one prepared create's maximum exposure."""

    chain_id: str
    operation_id: str
    prepared_operation_digest: str
    request_digest: str
    sdl_digest: str
    backend: BackendIdentity
    backend_policy_revision: str
    amount_uact: int
    source: str
    evidence_digest: str
    observed_at: int
    valid_until: int
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _nonempty_ascii(self.chain_id, "chain_id")
        _nonempty_ascii(self.operation_id, "operation_id", maximum=128)
        for name in (
            "prepared_operation_digest",
            "request_digest",
            "sdl_digest",
            "evidence_digest",
        ):
            _digest(getattr(self, name), name)
        if not isinstance(self.backend, BackendIdentity):
            raise ValueError("exposure backend must be typed")
        if (
            not isinstance(self.backend_policy_revision, str)
            or _REVISION.fullmatch(self.backend_policy_revision) is None
        ):
            raise ValueError("backend_policy_revision must be a lowercase 40-character revision")
        _count(self.amount_uact, "amount_uact")
        if self.amount_uact == 0:
            raise ValueError("amount_uact must be positive")
        _nonempty_ascii(self.source, "source")
        _time(self.observed_at, "observed_at")
        _time(self.valid_until, "valid_until")
        if self.valid_until < self.observed_at:
            raise ValueError("exposure validity cannot predate observation")


@dataclass(frozen=True)
class AdmissionRequest:
    prepared: PreparedCreate
    scope: AdmissionScope
    exposure: CreateExposureEvidence
    requested_at: int
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.prepared, PreparedCreate):
            raise ValueError("typed prepared operation is required")
        if not isinstance(self.scope, AdmissionScope):
            raise ValueError("scope must be typed")
        if not isinstance(self.exposure, CreateExposureEvidence):
            raise ValueError("typed exposure evidence is required")
        producer = self.prepared.producer
        lifecycle = self.prepared.lifecycle
        expected_scope = AdmissionScope(
            chain_id=self.exposure.chain_id,
            owner=self.prepared.owner_candidate.owner,
            producer_issuer=producer.issuer,
            repository_owner_id=producer.repository_owner_id,
            repository_id=producer.repository_id,
            repository=producer.repository,
            workload_class=lifecycle.workload_class,
        )
        if self.scope != expected_scope:
            raise ValueError("admission scope disagrees with typed prepared operation")
        expected_exposure = (
            self.scope.chain_id,
            self.operation_id,
            self.prepared_operation_digest,
            self.prepared.request_digest,
            self.prepared.sdl_digest,
            self.prepared.owner_candidate.backend,
        )
        actual_exposure = (
            self.exposure.chain_id,
            self.exposure.operation_id,
            self.exposure.prepared_operation_digest,
            self.exposure.request_digest,
            self.exposure.sdl_digest,
            self.exposure.backend,
        )
        if actual_exposure != expected_exposure:
            raise ValueError("exposure evidence disagrees with prepared operation or backend")
        _time(self.requested_at, "requested_at")
        if self.requested_at < max(self.prepared.prepared_at, self.exposure.observed_at):
            raise ValueError("admission request predates its prepared or exposure evidence")

    @property
    def operation_id(self) -> str:
        return self.prepared.operation_id

    @property
    def prepared_operation_digest(self) -> str:
        return canonical_journal_digest(self.prepared)


@dataclass(frozen=True)
class CapacityReservation:
    request: AdmissionRequest
    state: ReservationState
    state_evidence_digest: str
    bound_deployment: BoundDeployment | None = None
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.request, AdmissionRequest):
            raise ValueError("request must be typed")
        if not isinstance(self.state, ReservationState):
            raise ValueError("reservation state must be typed")
        _digest(self.state_evidence_digest, "state_evidence_digest")
        if self.bound_deployment is not None and not isinstance(
            self.bound_deployment, BoundDeployment
        ):
            raise ValueError("bound_deployment must be typed")
        if (
            self.state
            in {
                ReservationState.COMMITTED,
                ReservationState.EXECUTION_CLOSED,
                ReservationState.SETTLED,
            }
            and self.bound_deployment is None
        ):
            raise ValueError("committed reservation states require a bound deployment")
        if self.bound_deployment is not None and (
            self.bound_deployment.operation_id != self.request.operation_id
            or self.bound_deployment.subject.owner != self.request.scope.owner
            or self.bound_deployment.groups != self.request.prepared.groups
            or self.bound_deployment.group_population_digest
            != self.request.prepared.group_population_digest
        ):
            raise ValueError("bound deployment disagrees with reserved prepared operation")

    @property
    def active_slots(self) -> int:
        return int(
            self.state
            in {
                ReservationState.RESERVED,
                ReservationState.SUBMITTING,
                ReservationState.CREATE_OUTCOME_UNKNOWN,
                ReservationState.COMMITTED,
            }
        )

    @property
    def unresolved_slots(self) -> int:
        return int(
            self.state
            in {
                ReservationState.RESERVED,
                ReservationState.SUBMITTING,
                ReservationState.CREATE_OUTCOME_UNKNOWN,
            }
        )

    @property
    def financial_exposure_uact(self) -> int:
        if self.state in {ReservationState.NON_COMMIT_PROVEN, ReservationState.SETTLED}:
            return 0
        return self.request.exposure.amount_uact


@dataclass(frozen=True)
class AdmissionState:
    budgets: tuple[ScopeBudget, ...]
    reservations: tuple[CapacityReservation, ...] = ()
    revision: int = 0
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.budgets) is not tuple or not self.budgets:
            raise ValueError("at least one scope budget is required")
        if any(not isinstance(item, ScopeBudget) for item in self.budgets):
            raise ValueError("budgets must be typed")
        if len({item.scope for item in self.budgets}) != len(self.budgets):
            raise ValueError("scope budgets must be unique")
        if type(self.reservations) is not tuple or any(
            not isinstance(item, CapacityReservation) for item in self.reservations
        ):
            raise ValueError("reservations must be a typed tuple")
        operations = [item.request.operation_id for item in self.reservations]
        if len(set(operations)) != len(operations):
            raise ValueError("reservation operation IDs must be globally unique")
        _count(self.revision, "revision")


@dataclass(frozen=True)
class CapacityUsage:
    active_deployments: int
    unresolved_create_outcomes: int
    financial_exposure_uact: int


@dataclass(frozen=True)
class BudgetProjection:
    scope: OwnerBudgetScope | AdmissionScope
    usage_after: CapacityUsage


@dataclass(frozen=True)
class AdmissionDecision:
    disposition: AdmissionDisposition
    reason: AdmissionReason
    failed_scope: OwnerBudgetScope | AdmissionScope | None = None
    projections: tuple[BudgetProjection, ...] = ()


@dataclass(frozen=True, init=False)
class ReservationProposal:
    operation_id: str
    expected_revision: int
    expected_state_digest: str
    proposed_revision: int
    proposed_state_digest: str
    proposed_state: AdmissionState
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __init__(self) -> None:
        raise TypeError("ReservationProposal is issued only by reserve_capacity")

    @classmethod
    def _issue(cls, **values) -> ReservationProposal:
        proposal = object.__new__(cls)
        values["schema_version"] = ADMISSION_SCHEMA_VERSION
        for name, value in values.items():
            object.__setattr__(proposal, name, value)
        _nonempty_ascii(proposal.operation_id, "operation_id", maximum=128)
        _count(proposal.expected_revision, "expected_revision")
        _count(proposal.proposed_revision, "proposed_revision")
        for name in ("expected_state_digest", "proposed_state_digest"):
            _digest(getattr(proposal, name), name)
        if proposal.proposed_revision != proposal.expected_revision + 1:
            raise ValueError("proposal must advance exactly one revision")
        if proposal.proposed_state.revision != proposal.proposed_revision:
            raise ValueError("proposal revision disagrees with proposed state")
        if canonical_journal_digest(proposal.proposed_state) != proposal.proposed_state_digest:
            raise ValueError("proposal digest disagrees with proposed state")
        return proposal


@dataclass(frozen=True)
class AdmissionResult:
    decision: AdmissionDecision
    proposal: ReservationProposal | None


@dataclass(frozen=True)
class AuthenticatedBrokerEvidence:
    broker_identity: str
    issuer: str
    audience: str
    subject: str
    verifier_policy_revision: str
    authentication_evidence_digest: str
    verified_at: int
    valid_until: int

    def __post_init__(self) -> None:
        for name in ("broker_identity", "issuer", "audience", "subject"):
            _nonempty_ascii(getattr(self, name), name)
        if (
            not isinstance(self.verifier_policy_revision, str)
            or _REVISION.fullmatch(self.verifier_policy_revision) is None
        ):
            raise ValueError("verifier_policy_revision must be a lowercase 40-character revision")
        _digest(self.authentication_evidence_digest, "authentication_evidence_digest")
        _time(self.verified_at, "verified_at")
        _time(self.valid_until, "valid_until")
        if self.valid_until < self.verified_at:
            raise ValueError("broker authentication validity cannot predate verification")


@dataclass(frozen=True)
class PersistenceConfirmation:
    operation_id: str
    persisted_revision: int
    persisted_state_digest: str
    authenticated_broker: AuthenticatedBrokerEvidence
    evidence_digest: str
    persisted_at: int
    schema_version: int = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _nonempty_ascii(self.operation_id, "operation_id", maximum=128)
        _count(self.persisted_revision, "persisted_revision")
        _digest(self.persisted_state_digest, "persisted_state_digest")
        if not isinstance(self.authenticated_broker, AuthenticatedBrokerEvidence):
            raise ValueError("authenticated broker evidence is required")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.persisted_at, "persisted_at")


@dataclass(frozen=True)
class AdmissionPolicyBinding:
    scope: OwnerBudgetScope | AdmissionScope
    policy_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, (OwnerBudgetScope, AdmissionScope)):
            raise ValueError("policy scope must be typed")
        if (
            not isinstance(self.policy_revision, str)
            or _REVISION.fullmatch(self.policy_revision) is None
        ):
            raise ValueError("policy_revision must be a lowercase 40-character revision")


@dataclass(frozen=True)
class PermitPresentation:
    """Authenticated adapter evidence presented to redeem one current permit."""

    permit_digest: str
    presenter_identity: str
    audience: str
    current_policy_bindings: tuple[AdmissionPolicyBinding, ...]
    revocation_status: PermitRevocationStatus
    revocation_evidence_digest: str
    presented_at: int

    def __post_init__(self) -> None:
        _digest(self.permit_digest, "permit_digest")
        _nonempty_ascii(self.presenter_identity, "presenter_identity")
        _nonempty_ascii(self.audience, "audience")
        if type(self.current_policy_bindings) is not tuple or not self.current_policy_bindings:
            raise ValueError("current policy bindings must be a nonempty tuple")
        if not all(
            isinstance(item, AdmissionPolicyBinding) for item in self.current_policy_bindings
        ):
            raise ValueError("current policy bindings must be typed")
        if len({item.scope for item in self.current_policy_bindings}) != len(
            self.current_policy_bindings
        ):
            raise ValueError("current policy bindings must have unique scopes")
        if not isinstance(self.revocation_status, PermitRevocationStatus):
            raise ValueError("revocation status must be typed")
        _digest(self.revocation_evidence_digest, "revocation_evidence_digest")
        _time(self.presented_at, "presented_at")


@dataclass(frozen=True, init=False)
class CreatePermit:
    operation_id: str
    prepared_operation_digest: str
    state_revision: int
    state_digest: str
    broker_identity: str
    presenter_identity: str
    audience: str
    policy_bindings: tuple[AdmissionPolicyBinding, ...]
    valid_until: int
    persistence_evidence_digest: str
    issued_at: int
    schema_version: int

    def __init__(self) -> None:
        raise TypeError("CreatePermit is issued only by confirm_persisted_reservation")

    @classmethod
    def _issue(
        cls,
        *,
        operation_id: str,
        prepared_operation_digest: str,
        state_revision: int,
        state_digest: str,
        broker_identity: str,
        presenter_identity: str,
        audience: str,
        policy_bindings: tuple[AdmissionPolicyBinding, ...],
        valid_until: int,
        persistence_evidence_digest: str,
        issued_at: int,
    ) -> CreatePermit:
        permit = object.__new__(cls)
        values = {
            "operation_id": operation_id,
            "prepared_operation_digest": prepared_operation_digest,
            "state_revision": state_revision,
            "state_digest": state_digest,
            "broker_identity": broker_identity,
            "presenter_identity": presenter_identity,
            "audience": audience,
            "policy_bindings": policy_bindings,
            "valid_until": valid_until,
            "persistence_evidence_digest": persistence_evidence_digest,
            "issued_at": issued_at,
            "schema_version": ADMISSION_SCHEMA_VERSION,
        }
        for name, value in values.items():
            object.__setattr__(permit, name, value)
        _nonempty_ascii(permit.operation_id, "operation_id", maximum=128)
        _digest(permit.prepared_operation_digest, "prepared_operation_digest")
        _count(permit.state_revision, "state_revision")
        _digest(permit.state_digest, "state_digest")
        _nonempty_ascii(permit.broker_identity, "broker_identity")
        _nonempty_ascii(permit.presenter_identity, "presenter_identity")
        _nonempty_ascii(permit.audience, "audience")
        if (
            type(permit.policy_bindings) is not tuple
            or not permit.policy_bindings
            or not all(isinstance(item, AdmissionPolicyBinding) for item in permit.policy_bindings)
        ):
            raise ValueError("permit policy bindings must be a nonempty typed tuple")
        _time(permit.valid_until, "valid_until")
        if permit.valid_until < permit.issued_at:
            raise ValueError("permit validity cannot predate issuance")
        _digest(permit.persistence_evidence_digest, "persistence_evidence_digest")
        _time(permit.issued_at, "issued_at")
        return permit


@dataclass(frozen=True, init=False)
class PermitRedemptionProposal:
    operation_id: str
    permit_digest: str
    presentation_digest: str
    permit_issued_at: int
    expected_revision: int
    expected_state_digest: str
    proposed_revision: int
    proposed_state_digest: str
    proposed_state: AdmissionState

    def __init__(self) -> None:
        raise TypeError("PermitRedemptionProposal is issued only by redeem_create_permit")

    @classmethod
    def _issue(cls, **values) -> PermitRedemptionProposal:
        proposal = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(proposal, name, value)
        _nonempty_ascii(proposal.operation_id, "operation_id", maximum=128)
        _digest(proposal.permit_digest, "permit_digest")
        _digest(proposal.presentation_digest, "presentation_digest")
        _time(proposal.permit_issued_at, "permit_issued_at")
        _count(proposal.expected_revision, "expected_revision")
        _count(proposal.proposed_revision, "proposed_revision")
        _digest(proposal.expected_state_digest, "expected_state_digest")
        _digest(proposal.proposed_state_digest, "proposed_state_digest")
        if proposal.proposed_revision != proposal.expected_revision + 1:
            raise ValueError("redemption must advance exactly one revision")
        if (
            proposal.proposed_state.revision != proposal.proposed_revision
            or canonical_journal_digest(proposal.proposed_state) != proposal.proposed_state_digest
        ):
            raise ValueError("redemption proposal disagrees with proposed state")
        return proposal


class PermitRedemptionError(ValueError):
    """Raised when a permit cannot be atomically consumed exactly once."""


@dataclass(frozen=True, init=False)
class CreateSubmissionAuthorization:
    operation_id: str
    permit_digest: str
    state_revision: int
    state_digest: str
    broker_identity: str
    presenter_identity: str
    audience: str
    policy_bindings: tuple[AdmissionPolicyBinding, ...]
    valid_until: int
    redemption_evidence_digest: str
    issued_at: int

    def __init__(self) -> None:
        raise TypeError("CreateSubmissionAuthorization is issued only by confirm_redeemed_permit")

    @classmethod
    def _issue(cls, proposal, permit, confirmation):
        value = object.__new__(cls)
        fields = {
            "operation_id": proposal.operation_id,
            "permit_digest": proposal.permit_digest,
            "state_revision": proposal.proposed_revision,
            "state_digest": proposal.proposed_state_digest,
            "broker_identity": confirmation.authenticated_broker.broker_identity,
            "presenter_identity": permit.presenter_identity,
            "audience": permit.audience,
            "policy_bindings": permit.policy_bindings,
            "valid_until": permit.valid_until,
            "redemption_evidence_digest": confirmation.evidence_digest,
            "issued_at": confirmation.persisted_at,
        }
        for name, item in fields.items():
            object.__setattr__(value, name, item)
        return value


def canonical_admission_state_digest(state: AdmissionState) -> str:
    if not isinstance(state, AdmissionState):
        raise ValueError("typed admission state is required")
    return canonical_journal_digest(state)


def _reservation(state: AdmissionState, operation_id: str) -> CapacityReservation | None:
    return next(
        (item for item in state.reservations if item.request.operation_id == operation_id), None
    )


def _applies(scope: OwnerBudgetScope | AdmissionScope, leaf: AdmissionScope) -> bool:
    return scope == leaf or (
        isinstance(scope, OwnerBudgetScope)
        and scope.chain_id == leaf.chain_id
        and scope.owner == leaf.owner
    )


def _applicable_budgets(state: AdmissionState, leaf: AdmissionScope) -> tuple[ScopeBudget, ...]:
    return tuple(item for item in state.budgets if _applies(item.scope, leaf))


def _required_budgets(budgets: tuple[ScopeBudget, ...], leaf: AdmissionScope) -> bool:
    return {item.scope for item in budgets} == {OwnerBudgetScope(leaf.chain_id, leaf.owner), leaf}


def _policy_bindings(budgets: tuple[ScopeBudget, ...]) -> tuple[AdmissionPolicyBinding, ...]:
    return tuple(
        AdmissionPolicyBinding(item.scope, item.limits.admission_policy_revision)
        for item in budgets
    )


def _usage(state: AdmissionState, budget: ScopeBudget) -> CapacityUsage:
    relevant = [item for item in state.reservations if _applies(budget.scope, item.request.scope)]
    return CapacityUsage(
        budget.census.active_deployments + sum(item.active_slots for item in relevant),
        budget.census.unresolved_create_outcomes + sum(item.unresolved_slots for item in relevant),
        budget.census.financial_exposure_uact
        + sum(item.financial_exposure_uact for item in relevant),
    )


def _reservation_operation_ids(
    state: AdmissionState, scope: OwnerBudgetScope | AdmissionScope
) -> tuple[str, ...]:
    return tuple(
        sorted(
            item.request.operation_id
            for item in state.reservations
            if _applies(scope, item.request.scope)
        )
    )


def _census_binding_matches(state: AdmissionState, budget: ScopeBudget) -> bool:
    operation_ids = _reservation_operation_ids(state, budget.scope)
    return (
        budget.census.excluded_reservation_count == len(operation_ids)
        and budget.census.excluded_reservation_population_digest
        == canonical_reservation_population_digest(operation_ids)
    )


def capacity_usage(
    state: AdmissionState, scope: OwnerBudgetScope | AdmissionScope
) -> CapacityUsage:
    """Return census plus broker-held reservations for exactly one scope."""

    if not isinstance(state, AdmissionState) or not isinstance(
        scope, (OwnerBudgetScope, AdmissionScope)
    ):
        raise ValueError("typed admission state and budget scope are required")
    budget = next((item for item in state.budgets if item.scope == scope), None)
    if budget is None:
        raise ValueError("unknown admission scope")
    if not _census_binding_matches(state, budget):
        raise ValueError("census reservation-population binding mismatch")
    return _usage(state, budget)


def _hold(
    reason: AdmissionReason, scope: OwnerBudgetScope | AdmissionScope | None = None
) -> AdmissionResult:
    return AdmissionResult(AdmissionDecision(AdmissionDisposition.HOLD, reason, scope), None)


def reserve_capacity(
    state: AdmissionState,
    request: AdmissionRequest,
    *,
    expected_revision: int,
    now: int,
) -> AdmissionResult:
    """Return a CAS proposal after every applicable budget admits the request."""

    if not isinstance(state, AdmissionState) or not isinstance(request, AdmissionRequest):
        raise ValueError("typed admission state and request are required")
    _count(expected_revision, "expected_revision")
    _time(now, "now")
    budgets = _applicable_budgets(state, request.scope)
    if not _required_budgets(budgets, request.scope):
        return _hold(AdmissionReason.MISSING_REQUIRED_BUDGET)
    for budget in budgets:
        if not _census_binding_matches(state, budget):
            return _hold(AdmissionReason.RESERVATION_CENSUS_MISMATCH, budget.scope)
        if budget.limits.exposure_policy_revision != request.exposure.backend_policy_revision:
            return _hold(AdmissionReason.EXPOSURE_POLICY_MISMATCH, budget.scope)

    existing = _reservation(state, request.operation_id)
    if existing is not None:
        if existing.request == request:
            return AdmissionResult(
                AdmissionDecision(
                    AdmissionDisposition.RECONCILE,
                    AdmissionReason.RECONCILE_EXISTING,
                ),
                None,
            )
        return _hold(AdmissionReason.OPERATION_CONFLICT)
    if expected_revision != state.revision:
        return _hold(AdmissionReason.STALE_REVISION)
    if not request.exposure.observed_at <= now <= request.exposure.valid_until:
        return _hold(AdmissionReason.EXPOSURE_EVIDENCE_NOT_CURRENT)

    projections: list[BudgetProjection] = []
    for budget in budgets:
        census = budget.census
        statuses = (
            (census.journal_status, AdmissionReason.JOURNAL_UNHEALTHY),
            (census.recovery_reader_status, AdmissionReason.RECOVERY_READER_UNHEALTHY),
            (census.chain_status, AdmissionReason.CHAIN_CENSUS_UNHEALTHY),
            (census.accounting_status, AdmissionReason.ACCOUNTING_CENSUS_UNHEALTHY),
        )
        for status, reason in statuses:
            if status is not CensusStatus.VERIFIED:
                return _hold(reason, budget.scope)
        if (
            request.requested_at > now
            or not census.observed_at <= now <= census.valid_until
            or now > budget.limits.valid_until
        ):
            return _hold(AdmissionReason.CENSUS_NOT_CURRENT, budget.scope)
        usage = _usage(state, budget)
        projected = CapacityUsage(
            usage.active_deployments + 1,
            usage.unresolved_create_outcomes + 1,
            usage.financial_exposure_uact + request.exposure.amount_uact,
        )
        checks = (
            (
                projected.active_deployments,
                budget.limits.max_active_deployments,
                AdmissionReason.ACTIVE_DEPLOYMENT_LIMIT,
            ),
            (
                projected.unresolved_create_outcomes,
                budget.limits.max_unresolved_create_outcomes,
                AdmissionReason.UNRESOLVED_OUTCOME_LIMIT,
            ),
            (
                projected.financial_exposure_uact,
                budget.limits.max_financial_exposure_uact,
                AdmissionReason.FINANCIAL_EXPOSURE_LIMIT,
            ),
        )
        for value, limit, reason in checks:
            if value > limit:
                return _hold(reason, budget.scope)
        projections.append(BudgetProjection(budget.scope, projected))

    reservation = CapacityReservation(
        request,
        ReservationState.RESERVED,
        request.prepared_operation_digest,
    )
    reservations = state.reservations + (reservation,)
    updated_budgets = []
    for budget in state.budgets:
        if budget in budgets:
            operation_ids = tuple(
                sorted(
                    item.request.operation_id
                    for item in reservations
                    if _applies(budget.scope, item.request.scope)
                )
            )
            budget = replace(
                budget,
                census=replace(
                    budget.census,
                    excluded_reservation_count=len(operation_ids),
                    excluded_reservation_population_digest=(
                        canonical_reservation_population_digest(operation_ids)
                    ),
                ),
            )
        updated_budgets.append(budget)
    proposed_state = replace(
        state,
        budgets=tuple(updated_budgets),
        reservations=reservations,
        revision=state.revision + 1,
    )
    proposal = ReservationProposal._issue(
        operation_id=request.operation_id,
        expected_revision=state.revision,
        expected_state_digest=canonical_admission_state_digest(state),
        proposed_revision=proposed_state.revision,
        proposed_state_digest=canonical_admission_state_digest(proposed_state),
        proposed_state=proposed_state,
    )
    return AdmissionResult(
        AdmissionDecision(
            AdmissionDisposition.PROPOSED,
            AdmissionReason.PROPOSED,
            projections=tuple(projections),
        ),
        proposal,
    )


def confirm_persisted_reservation(
    proposal: ReservationProposal,
    original_state: AdmissionState,
    request: AdmissionRequest,
    persisted_state: AdmissionState,
    confirmation: PersistenceConfirmation,
) -> CreatePermit:
    """Issue a permit only after a broker confirms the exact CAS result."""

    if not isinstance(proposal, ReservationProposal) or not isinstance(
        confirmation, PersistenceConfirmation
    ):
        raise ValueError("typed proposal and persistence confirmation are required")
    if not isinstance(original_state, AdmissionState) or not isinstance(
        persisted_state, AdmissionState
    ):
        raise ValueError("typed original and persisted states are required")
    if not isinstance(request, AdmissionRequest):
        raise ValueError("typed admission request is required")
    derived = reserve_capacity(
        original_state,
        request,
        expected_revision=proposal.expected_revision,
        now=confirmation.persisted_at,
    )
    if derived.proposal != proposal:
        raise PersistenceConfirmationError(
            "reservation proposal was not derived from the supplied policy state"
        )
    persisted_digest = canonical_admission_state_digest(persisted_state)
    if (
        confirmation.operation_id != proposal.operation_id
        or confirmation.persisted_revision != proposal.proposed_revision
        or confirmation.persisted_state_digest != proposal.proposed_state_digest
        or persisted_state != proposal.proposed_state
        or persisted_digest != confirmation.persisted_state_digest
    ):
        raise PersistenceConfirmationError("persistence confirmation disagrees with CAS proposal")
    reservation = _reservation(persisted_state, proposal.operation_id)
    if reservation is None or reservation.state is not ReservationState.RESERVED:
        raise PersistenceConfirmationError("persisted proposal lacks the exact reserved operation")
    budgets = _applicable_budgets(persisted_state, reservation.request.scope)
    if not _required_budgets(budgets, reservation.request.scope) or any(
        not _census_binding_matches(persisted_state, budget) for budget in budgets
    ):
        raise PersistenceConfirmationError("persisted reservation budget binding is incomplete")
    if confirmation.persisted_at < reservation.request.requested_at:
        raise PersistenceConfirmationError("persistence confirmation predates reservation")
    broker = confirmation.authenticated_broker
    if not broker.verified_at <= confirmation.persisted_at <= broker.valid_until:
        raise PersistenceConfirmationError("broker authentication is not current")
    return CreatePermit._issue(
        operation_id=proposal.operation_id,
        prepared_operation_digest=reservation.request.prepared_operation_digest,
        state_revision=proposal.proposed_revision,
        state_digest=proposal.proposed_state_digest,
        broker_identity=broker.broker_identity,
        presenter_identity=reservation.request.prepared.producer.subject,
        audience=CREATE_PERMIT_AUDIENCE,
        policy_bindings=_policy_bindings(budgets),
        valid_until=min(
            broker.valid_until,
            reservation.request.exposure.valid_until,
            *(budget.limits.valid_until for budget in budgets),
            *(budget.census.valid_until for budget in budgets),
        ),
        persistence_evidence_digest=confirmation.evidence_digest,
        issued_at=confirmation.persisted_at,
    )


def redeem_create_permit(
    state: AdmissionState,
    permit: CreatePermit,
    presentation: PermitPresentation,
    *,
    expected_revision: int,
) -> PermitRedemptionProposal:
    """Propose the one-time CAS that consumes a permit before network I/O."""

    if (
        not isinstance(state, AdmissionState)
        or not isinstance(permit, CreatePermit)
        or not isinstance(presentation, PermitPresentation)
    ):
        raise ValueError("typed admission state, create permit, and presentation are required")
    _count(expected_revision, "expected_revision")
    if expected_revision != state.revision or permit.state_revision > state.revision:
        raise PermitRedemptionError("permit does not bind the current persisted revision")
    reservation = _reservation(state, permit.operation_id)
    if reservation is None or (
        reservation.request.prepared_operation_digest != permit.prepared_operation_digest
    ):
        raise PermitRedemptionError("permit does not bind the reserved prepared operation")
    if reservation.state is not ReservationState.RESERVED:
        raise PermitRedemptionError("permit is already redeemed or no longer submittable")
    permit_digest = canonical_journal_digest(permit)
    current_bindings = _policy_bindings(_applicable_budgets(state, reservation.request.scope))
    if (
        presentation.permit_digest != permit_digest
        or presentation.presenter_identity != permit.presenter_identity
        or presentation.audience != permit.audience
    ):
        raise PermitRedemptionError("permit presentation disagrees with presenter or audience")
    if presentation.revocation_status is not PermitRevocationStatus.ACTIVE:
        raise PermitRedemptionError("permit is revoked")
    if not permit.issued_at <= presentation.presented_at <= permit.valid_until:
        raise PermitRedemptionError("permit presentation is not current")
    if (
        presentation.current_policy_bindings != permit.policy_bindings
        or current_bindings != permit.policy_bindings
    ):
        raise PermitRedemptionError("admission policy revision changed before redemption")
    presentation_digest = canonical_journal_digest(presentation)
    updated = replace(
        reservation,
        state=ReservationState.SUBMITTING,
        state_evidence_digest=presentation_digest,
    )
    reservations = tuple(updated if item is reservation else item for item in state.reservations)
    proposed_state = replace(state, reservations=reservations, revision=state.revision + 1)
    return PermitRedemptionProposal._issue(
        operation_id=permit.operation_id,
        permit_digest=permit_digest,
        presentation_digest=presentation_digest,
        permit_issued_at=permit.issued_at,
        expected_revision=state.revision,
        expected_state_digest=canonical_admission_state_digest(state),
        proposed_revision=proposed_state.revision,
        proposed_state_digest=canonical_admission_state_digest(proposed_state),
        proposed_state=proposed_state,
    )


def confirm_redeemed_permit(
    proposal: PermitRedemptionProposal,
    permit: CreatePermit,
    presentation: PermitPresentation,
    pre_redemption_state: AdmissionState,
    persisted_state: AdmissionState,
    confirmation: PersistenceConfirmation,
) -> CreateSubmissionAuthorization:
    """Issue network authority only after authenticated confirmation of redemption."""

    if not isinstance(proposal, PermitRedemptionProposal):
        raise ValueError("typed permit redemption proposal is required")
    if not isinstance(permit, CreatePermit) or not isinstance(
        pre_redemption_state, AdmissionState
    ):
        raise ValueError("typed permit and pre-redemption state are required")
    if not isinstance(persisted_state, AdmissionState) or not isinstance(
        confirmation, PersistenceConfirmation
    ):
        raise ValueError("typed persisted state and confirmation are required")
    derived = redeem_create_permit(
        pre_redemption_state,
        permit,
        presentation,
        expected_revision=proposal.expected_revision,
    )
    if derived != proposal:
        raise PersistenceConfirmationError(
            "redemption proposal was not derived from the supplied permit and state"
        )
    if (
        confirmation.operation_id != proposal.operation_id
        or confirmation.persisted_revision != proposal.proposed_revision
        or confirmation.persisted_state_digest != proposal.proposed_state_digest
        or persisted_state != proposal.proposed_state
        or canonical_admission_state_digest(persisted_state) != proposal.proposed_state_digest
    ):
        raise PersistenceConfirmationError("redemption confirmation disagrees with CAS proposal")
    reservation = _reservation(persisted_state, proposal.operation_id)
    if reservation is None or (
        reservation.state is not ReservationState.SUBMITTING
        or reservation.state_evidence_digest != proposal.presentation_digest
    ):
        raise PersistenceConfirmationError("persisted state lacks exact permit redemption")
    budgets = _applicable_budgets(persisted_state, reservation.request.scope)
    if not _required_budgets(budgets, reservation.request.scope) or any(
        not _census_binding_matches(persisted_state, budget) for budget in budgets
    ):
        raise PersistenceConfirmationError("persisted redemption budget binding is incomplete")
    broker = confirmation.authenticated_broker
    if (
        broker.broker_identity != permit.broker_identity
        or confirmation.persisted_at < presentation.presented_at
        or not broker.verified_at <= confirmation.persisted_at <= broker.valid_until
    ):
        raise PersistenceConfirmationError("broker authentication is not current")
    if confirmation.persisted_at > permit.valid_until:
        raise PersistenceConfirmationError("permit expired before redemption confirmation")
    return CreateSubmissionAuthorization._issue(proposal, permit, confirmation)


_ALLOWED_TRANSITIONS = {
    ReservationState.RESERVED: frozenset(),
    ReservationState.SUBMITTING: frozenset(
        {
            ReservationState.CREATE_OUTCOME_UNKNOWN,
            ReservationState.NON_COMMIT_PROVEN,
            ReservationState.COMMITTED,
        }
    ),
    ReservationState.CREATE_OUTCOME_UNKNOWN: frozenset(
        {ReservationState.NON_COMMIT_PROVEN, ReservationState.COMMITTED}
    ),
    ReservationState.COMMITTED: frozenset({ReservationState.EXECUTION_CLOSED}),
    ReservationState.EXECUTION_CLOSED: frozenset({ReservationState.SETTLED}),
    ReservationState.NON_COMMIT_PROVEN: frozenset(),
    ReservationState.SETTLED: frozenset(),
}


def _outcome_target(
    reservation: CapacityReservation,
    outcome: CreateOutcome,
) -> tuple[ReservationState, BoundDeployment | None]:
    prepared = reservation.request.prepared
    if outcome.operation_id != prepared.operation_id:
        raise ReservationTransitionError("create outcome belongs to another operation")
    if outcome.kind is CreateOutcomeKind.REJECTED_BEFORE_SEND:
        evidence = outcome.rejection
        if not isinstance(evidence, RejectionEvidence) or (
            evidence.backend != prepared.owner_candidate.backend
        ):
            raise ReservationTransitionError("rejection backend disagrees with prepared operation")
        return ReservationState.NON_COMMIT_PROVEN, None
    if outcome.kind is CreateOutcomeKind.FALLBACK_SAFE:
        evidence = outcome.non_commit
        if not isinstance(evidence, NonCommitEvidence) or (
            evidence.backend != prepared.owner_candidate.backend
            or evidence.exact_owner != prepared.owner_candidate.owner
            or evidence.group_population_digest != prepared.group_population_digest
        ):
            raise ReservationTransitionError(
                "non-commit evidence disagrees with prepared operation"
            )
        return ReservationState.NON_COMMIT_PROVEN, None
    if outcome.kind is CreateOutcomeKind.UNKNOWN:
        if not isinstance(outcome.uncertainty, UncertaintyEvidence):
            raise ReservationTransitionError("unknown outcome lacks typed uncertainty evidence")
        return ReservationState.CREATE_OUTCOME_UNKNOWN, None
    deployment = outcome.deployment
    if not isinstance(deployment, BoundDeployment) or (
        deployment.subject.owner != prepared.owner_candidate.owner
        or deployment.groups != prepared.groups
        or deployment.group_population_digest != prepared.group_population_digest
    ):
        raise ReservationTransitionError("committed deployment disagrees with prepared operation")
    return ReservationState.COMMITTED, deployment


def transition_reservation(
    state: AdmissionState,
    evidence: CreateOutcome | ExecutionClosure | SettlementEvidence,
    *,
    expected_revision: int,
) -> AdmissionState:
    """Advance release state from typed, exactly bound lifecycle evidence."""

    if not isinstance(state, AdmissionState) or not isinstance(
        evidence, (CreateOutcome, ExecutionClosure, SettlementEvidence)
    ):
        raise ValueError("typed admission state and lifecycle evidence are required")
    _count(expected_revision, "expected_revision")
    reservation = _reservation(state, evidence.operation_id)
    if reservation is None:
        raise ReservationTransitionError("unknown reservation operation")
    deployment = reservation.bound_deployment
    if isinstance(evidence, CreateOutcome):
        target, deployment = _outcome_target(reservation, evidence)
    elif isinstance(evidence, ExecutionClosure):
        if deployment is None or (
            evidence.subject != deployment.subject
            or evidence.group_population_digest != deployment.group_population_digest
            or evidence.chain_id != deployment.binding_evidence.chain_id
        ):
            raise ReservationTransitionError("closure evidence disagrees with bound deployment")
        target = ReservationState.EXECUTION_CLOSED
    else:
        if evidence.state is not SettlementState.SETTLED:
            raise ReservationTransitionError("financial release requires measured settlement")
        if deployment is None or evidence.subject != deployment.subject:
            raise ReservationTransitionError("settlement evidence disagrees with bound deployment")
        target = ReservationState.SETTLED
    evidence_digest = canonical_journal_digest(evidence)
    if reservation.state is target:
        if reservation.state_evidence_digest == evidence_digest:
            return state
        raise ReservationTransitionError("reservation transition evidence would be rewritten")
    if expected_revision != state.revision:
        raise ReservationTransitionError("stale admission-state revision")
    if target not in _ALLOWED_TRANSITIONS[reservation.state]:
        raise ReservationTransitionError("invalid reservation transition")
    updated = replace(
        reservation,
        state=target,
        state_evidence_digest=evidence_digest,
        bound_deployment=deployment,
    )
    reservations = tuple(updated if item is reservation else item for item in state.reservations)
    return replace(state, reservations=reservations, revision=state.revision + 1)
