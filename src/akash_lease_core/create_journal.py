"""Immutable, sans-I/O contract for durable Akash create journals.

The values in this module carry evidence; they do not obtain or persist it.  An
adapter is responsible for authenticating producers, resolving owners, reading
the chain and atomically storing the returned journal revision.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import TypeAlias

from .chain_identity import DeploymentKey, is_canonical_akash_owner
from .workload_identity import CLASSES, MAX_UTC_UNIX_SECONDS

JOURNAL_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_POSITIVE = re.compile(r"^[1-9][0-9]*$")


class BackendKind(str, Enum):
    """The two submission trust boundaries understood by the contract."""

    SELF_MANAGED = "self_managed"
    MEDIATED = "mediated"


@dataclass(frozen=True)
class BackendIdentity:
    """A stable backend kind and non-secret adapter identity."""

    kind: BackendKind
    identifier: str
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.kind, BackendKind):
            raise ValueError("backend kind must be typed")
        _nonempty(self.identifier, "backend identifier")
        if not self.identifier.isascii() or len(self.identifier) > 256:
            raise ValueError("backend identifier must be at most 256 ASCII characters")


class OwnerEvidenceKind(str, Enum):
    VERIFIED_SIGNER = "verified_signer"
    AUTHENTICATED_MEDIATOR = "authenticated_mediator"


class CreateOutcomeKind(str, Enum):
    REJECTED_BEFORE_SEND = "rejected_before_send"
    FALLBACK_SAFE = "fallback_safe"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class OutcomeReasonCode(str, Enum):
    """Stable, machine-readable reasons; explanatory text stays adapter-owned."""

    INTENT_REJECTED = "intent_rejected"
    SIGNING_REJECTED = "signing_rejected"
    MEDIATOR_REJECTED = "mediator_rejected"
    SUBMISSION_PROVEN_ABSENT = "submission_proven_absent"
    DEPLOYMENT_PROVEN_ABSENT = "deployment_proven_absent"
    TRANSACTION_EVENT_BOUND = "transaction_event_bound"
    EXACT_CHAIN_READ_BOUND = "exact_chain_read_bound"
    BROAD_EXCEPTION = "broad_exception"
    STARTED_PROCESS_TIMEOUT = "started_process_timeout"
    HTTP_READ_TIMEOUT = "http_read_timeout"
    HTTP_WRITE_TIMEOUT = "http_write_timeout"
    TRANSPORT_ERROR = "transport_error"
    RESPONSE_LOST = "response_lost"
    BACKEND_ROTATION = "backend_rotation"
    PROVIDER_ROTATION = "provider_rotation"
    RECONCILIATION_INCOMPLETE = "reconciliation_incomplete"


class JournalState(str, Enum):
    PREPARED = "prepared"
    SUBMITTED = "submitted"
    REJECTED_BEFORE_SEND = "rejected_before_send"
    FALLBACK_SAFE = "fallback_safe"
    CREATE_OUTCOME_UNKNOWN = "create_outcome_unknown"
    CREATED = "created"
    HANDOFF_FAILED = "handoff_failed"
    CREATOR_ROLLBACK_REQUESTED = "creator_rollback_requested"
    EXECUTION_CLOSED = "execution_closed"
    SETTLEMENT = "settlement"


class SettlementState(str, Enum):
    UNMEASURED = "unmeasured"
    UNSETTLED = "unsettled"
    SETTLED = "settled"


class ExecutionClosureProofMode(str, Enum):
    """Finality mode admitted for a durable post-close observation bundle."""

    EXACT_FINALIZED_HEIGHT = "exact_finalized_height"


class TransitionError(ValueError):
    """Raised when an entry would rewrite or skip journal history."""


def _nonempty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _time(value: object, name: str) -> None:
    if type(value) is not int or not 0 < value <= MAX_UTC_UNIX_SECONDS:
        raise ValueError(f"{name} must be canonical UTC Unix seconds")


def _version(value: object) -> None:
    if type(value) is not int or value != JOURNAL_SCHEMA_VERSION:
        raise ValueError("unsupported create-journal schema version")


def _operation(value: object) -> None:
    _nonempty(value, "operation_id")
    if not value.isascii() or len(value) > 128:
        raise ValueError("operation_id must be at most 128 ASCII characters")


def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": type(value).__name__,
            **{field.name: _canonical(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if value is None or type(value) in {bool, int, str}:
        return value
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_journal_bytes(value: object) -> bytes:
    """Return deterministic UTF-8 JSON for a typed journal value."""

    if not is_dataclass(value) or isinstance(value, type):
        raise ValueError("canonical journal serialization requires a typed dataclass value")
    return json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def canonical_journal_digest(value: object) -> str:
    """Return the lowercase SHA-256 digest of canonical journal bytes."""

    return hashlib.sha256(canonical_journal_bytes(value)).hexdigest()


def canonical_payload_digest(payload: bytes) -> str:
    """Digest exact request, SDL, transaction, or evidence bytes."""

    if not isinstance(payload, bytes):
        raise ValueError("canonical payload digest requires bytes")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ProducerProvenance:
    repository: str
    repository_id: str
    repository_owner_id: str
    issuer: str
    audience: str
    subject: str
    workflow_ref: str
    workflow_sha: str
    ref: str
    jti: str
    authentication_evidence_digest: str
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in (
            "repository",
            "issuer",
            "audience",
            "subject",
            "workflow_ref",
            "ref",
            "jti",
        ):
            _nonempty(getattr(self, name), name)
        if self.repository.count("/") != 1:
            raise ValueError("repository must be an owner/name identity")
        if _POSITIVE.fullmatch(self.repository_id) is None:
            raise ValueError("repository_id must be a positive canonical integer string")
        if _POSITIVE.fullmatch(self.repository_owner_id) is None:
            raise ValueError("repository_owner_id must be a positive canonical integer string")
        if _REVISION.fullmatch(self.workflow_sha) is None:
            raise ValueError("workflow_sha must be a lowercase 40-character revision")
        _digest(self.authentication_evidence_digest, "authentication_evidence_digest")


@dataclass(frozen=True)
class LifecycleIdentity:
    repository: str
    workload_class: str
    run: int | None = None
    run_attempt: int | None = None
    release: str | None = None
    expires: int | None = None
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _nonempty(self.repository, "repository")
        if self.workload_class not in CLASSES:
            raise ValueError("unknown workload class")
        if self.workload_class.startswith("ci-"):
            if (
                type(self.run) is not int
                or self.run <= 0
                or type(self.run_attempt) is not int
                or self.run_attempt <= 0
                or self.release is not None
                or self.expires is not None
            ):
                raise ValueError("CI lifecycle requires run/attempt and no release/expiry")
        elif (
            self.run is not None
            or self.run_attempt is not None
            or not isinstance(self.release, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._]{0,63}", self.release) is None
            or (self.expires is not None and self.workload_class != "staging-payload")
        ):
            raise ValueError("payload lifecycle requires a canonical release")
        if self.expires is not None:
            _time(self.expires, "expires")


@dataclass(frozen=True)
class OwnerCandidate:
    backend: BackendIdentity
    owner: str
    evidence_kind: OwnerEvidenceKind
    evidence_source: str
    evidence_digest: str
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.backend, BackendIdentity):
            raise ValueError("backend must be a canonical BackendIdentity")
        if not is_canonical_akash_owner(self.owner):
            raise ValueError("owner must be a canonical Akash account")
        expected = {
            BackendKind.SELF_MANAGED: OwnerEvidenceKind.VERIFIED_SIGNER,
            BackendKind.MEDIATED: OwnerEvidenceKind.AUTHENTICATED_MEDIATOR,
        }
        if self.evidence_kind is not expected[self.backend.kind]:
            raise ValueError("owner evidence kind does not match backend kind")
        _nonempty(self.evidence_source, "evidence_source")
        _digest(self.evidence_digest, "evidence_digest")


@dataclass(frozen=True, order=True)
class PreparedGroup:
    gseq: int
    group_name: str

    def __post_init__(self) -> None:
        if type(self.gseq) is not int or self.gseq <= 0:
            raise ValueError("gseq must be a positive canonical integer")
        _nonempty(self.group_name, "group_name")
        if (
            not self.group_name.isascii()
            or self.group_name != self.group_name.strip()
            or any(character.isspace() for character in self.group_name)
        ):
            raise ValueError("group_name must be canonical non-whitespace ASCII")


def canonical_group_population_digest(groups: tuple[PreparedGroup, ...]) -> str:
    if type(groups) is not tuple or not groups:
        raise ValueError("complete prepared group tuple is required")
    if any(not isinstance(group, PreparedGroup) for group in groups):
        raise ValueError("prepared groups must be typed")
    if tuple(group.gseq for group in groups) != tuple(range(1, len(groups) + 1)):
        raise ValueError("prepared groups must be the ordered complete 1..N population")
    return hashlib.sha256(
        json.dumps(
            [[group.gseq, group.group_name] for group in groups],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class PreparedCreate:
    operation_id: str
    operation_ordinal: int
    producer: ProducerProvenance
    lifecycle: LifecycleIdentity
    owner_candidate: OwnerCandidate
    request_digest: str
    sdl_digest: str
    groups: tuple[PreparedGroup, ...]
    group_population_digest: str
    source_revision: str
    prepared_at: int
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _operation(self.operation_id)
        if type(self.operation_ordinal) is not int or self.operation_ordinal <= 0:
            raise ValueError("operation_ordinal must be a positive canonical integer")
        if not isinstance(self.producer, ProducerProvenance):
            raise ValueError("authenticated producer provenance is required")
        if not isinstance(self.lifecycle, LifecycleIdentity):
            raise ValueError("typed lifecycle identity is required")
        if self.lifecycle.repository != self.producer.repository:
            raise ValueError("lifecycle repository disagrees with authenticated producer")
        if not isinstance(self.owner_candidate, OwnerCandidate):
            raise ValueError("typed owner candidate evidence is required")
        _digest(self.request_digest, "request_digest")
        _digest(self.sdl_digest, "sdl_digest")
        expected = canonical_group_population_digest(self.groups)
        if self.group_population_digest != expected:
            raise ValueError("group population digest disagrees with complete groups")
        if _REVISION.fullmatch(self.source_revision) is None:
            raise ValueError("source_revision must be a lowercase 40-character revision")
        _time(self.prepared_at, "prepared_at")


@dataclass(frozen=True)
class SubmissionEvidence:
    operation_id: str
    backend: BackendIdentity
    submitted_request_digest: str
    submitted_at: int
    evidence_source: str
    evidence_digest: str
    transaction_hash: str | None = None
    account_sequence: int | None = None
    signed_bytes: bytes | None = None
    submission_token: str | None = None
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _operation(self.operation_id)
        if not isinstance(self.backend, BackendIdentity):
            raise ValueError("backend must be a canonical BackendIdentity")
        _digest(self.submitted_request_digest, "submitted_request_digest")
        _time(self.submitted_at, "submitted_at")
        _nonempty(self.evidence_source, "evidence_source")
        _digest(self.evidence_digest, "evidence_digest")
        if self.backend.kind is BackendKind.SELF_MANAGED:
            if (
                not isinstance(self.signed_bytes, bytes)
                or not self.signed_bytes
                or not isinstance(self.transaction_hash, str)
                or _DIGEST.fullmatch(self.transaction_hash) is None
                or canonical_payload_digest(self.signed_bytes) != self.transaction_hash
                or type(self.account_sequence) is not int
                or self.account_sequence < 0
                or self.submission_token is not None
            ):
                raise ValueError(
                    "self-managed submission requires matching signed bytes, tx hash and sequence"
                )
        elif (
            self.transaction_hash is not None
            or self.account_sequence is not None
            or self.signed_bytes is not None
            or not isinstance(self.submission_token, str)
            or not self.submission_token.strip()
        ):
            raise ValueError("mediated submission requires only a server-issued submission token")


@dataclass(frozen=True)
class RejectionEvidence:
    operation_id: str
    backend: BackendIdentity
    before_send: bool
    source: str
    evidence_digest: str
    observed_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.backend, BackendIdentity) or self.before_send is not True:
            raise ValueError("typed rejection must prove it occurred before send")
        _nonempty(self.source, "source")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")


@dataclass(frozen=True)
class NonCommitEvidence:
    operation_id: str
    backend: BackendIdentity
    submission_token: str | None
    transaction_hash: str | None
    exact_owner: str
    group_population_digest: str
    source_a: str
    source_b: str
    evidence_digest: str
    observed_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.backend, BackendIdentity):
            raise ValueError("backend must be a canonical BackendIdentity")
        if self.backend.kind is BackendKind.SELF_MANAGED:
            if self.submission_token is not None:
                raise ValueError("self-managed non-commit proof cannot carry a server token")
            if self.transaction_hash is not None:
                _digest(self.transaction_hash, "transaction_hash")
        elif self.transaction_hash is not None:
            raise ValueError("mediated non-commit proof cannot carry a transaction hash")
        elif self.submission_token is not None:
            _nonempty(self.submission_token, "submission_token")
        if not is_canonical_akash_owner(self.exact_owner):
            raise ValueError("non-commit proof requires the exact canonical owner")
        _digest(self.group_population_digest, "group_population_digest")
        _nonempty(self.source_a, "source_a")
        _nonempty(self.source_b, "source_b")
        if self.source_a == self.source_b:
            raise ValueError("non-commit proof requires two independent sources")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")


@dataclass(frozen=True)
class UncertaintyEvidence:
    operation_id: str
    source: str
    evidence_digest: str
    observed_at: int
    response_dseq: str | None = None

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        _nonempty(self.source, "source")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")
        if self.response_dseq is not None:
            _nonempty(self.response_dseq, "response_dseq")


@dataclass(frozen=True)
class TransactionEventEvidence:
    operation_id: str
    subject: DeploymentKey
    transaction_hash: str
    event_index: int
    block_height: int
    chain_id: str
    group_population_digest: str
    source: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey):
            raise ValueError("transaction event requires an exact deployment subject")
        _digest(self.transaction_hash, "transaction_hash")
        if type(self.event_index) is not int or self.event_index < 0:
            raise ValueError("event_index must be a nonnegative integer")
        if type(self.block_height) is not int or self.block_height <= 0:
            raise ValueError("block_height must be a positive integer")
        _nonempty(self.chain_id, "chain_id")
        _digest(self.group_population_digest, "group_population_digest")
        _nonempty(self.source, "source")
        _digest(self.evidence_digest, "evidence_digest")


@dataclass(frozen=True)
class ExactChainReadEvidence:
    operation_id: str
    subject: DeploymentKey
    chain_id: str
    block_height: int
    groups: tuple[PreparedGroup, ...]
    group_population_digest: str
    source: str
    evidence_digest: str
    observed_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey):
            raise ValueError("chain read requires an exact deployment subject")
        _nonempty(self.chain_id, "chain_id")
        if type(self.block_height) is not int or self.block_height <= 0:
            raise ValueError("block_height must be a positive integer")
        if self.group_population_digest != canonical_group_population_digest(self.groups):
            raise ValueError("chain-read group digest disagrees with its complete population")
        _nonempty(self.source, "source")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")


BindingEvidence: TypeAlias = TransactionEventEvidence | ExactChainReadEvidence


@dataclass(frozen=True)
class BoundDeployment:
    operation_id: str
    subject: DeploymentKey
    groups: tuple[PreparedGroup, ...]
    group_population_digest: str
    binding_evidence: BindingEvidence
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey):
            raise ValueError("bound deployment requires an exact DeploymentKey")
        if self.group_population_digest != canonical_group_population_digest(self.groups):
            raise ValueError("bound group digest disagrees with complete groups")
        if not isinstance(
            self.binding_evidence, (TransactionEventEvidence, ExactChainReadEvidence)
        ):
            raise ValueError("server or locally copied identity is not chain binding evidence")
        if (
            self.binding_evidence.operation_id != self.operation_id
            or self.binding_evidence.subject != self.subject
            or self.binding_evidence.group_population_digest != self.group_population_digest
        ):
            raise ValueError("binding evidence disagrees with operation, subject or groups")
        if isinstance(self.binding_evidence, ExactChainReadEvidence) and (
            self.binding_evidence.groups != self.groups
        ):
            raise ValueError("exact chain-read population disagrees with bound deployment")


_REJECTED_REASONS = {
    OutcomeReasonCode.INTENT_REJECTED,
    OutcomeReasonCode.SIGNING_REJECTED,
    OutcomeReasonCode.MEDIATOR_REJECTED,
}
_SAFE_REASONS = {
    OutcomeReasonCode.SUBMISSION_PROVEN_ABSENT,
    OutcomeReasonCode.DEPLOYMENT_PROVEN_ABSENT,
}
_COMMITTED_REASONS = {
    OutcomeReasonCode.TRANSACTION_EVENT_BOUND,
    OutcomeReasonCode.EXACT_CHAIN_READ_BOUND,
}
_UNKNOWN_REASONS = set(OutcomeReasonCode) - _REJECTED_REASONS - _SAFE_REASONS - _COMMITTED_REASONS


@dataclass(frozen=True)
class CreateOutcome:
    operation_id: str
    kind: CreateOutcomeKind
    reason_code: OutcomeReasonCode
    rejection: RejectionEvidence | None = None
    non_commit: NonCommitEvidence | None = None
    uncertainty: UncertaintyEvidence | None = None
    deployment: BoundDeployment | None = None
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _operation(self.operation_id)
        if not isinstance(self.kind, CreateOutcomeKind) or not isinstance(
            self.reason_code, OutcomeReasonCode
        ):
            raise ValueError("create outcome requires typed kind and reason code")
        expected = {
            CreateOutcomeKind.REJECTED_BEFORE_SEND: (_REJECTED_REASONS, self.rejection),
            CreateOutcomeKind.FALLBACK_SAFE: (_SAFE_REASONS, self.non_commit),
            CreateOutcomeKind.COMMITTED: (_COMMITTED_REASONS, self.deployment),
            CreateOutcomeKind.UNKNOWN: (_UNKNOWN_REASONS, self.uncertainty),
        }
        reasons, evidence = expected[self.kind]
        if self.reason_code not in reasons or evidence is None:
            raise ValueError("outcome reason or evidence does not match outcome kind")
        populated = tuple(
            item
            for item in (self.rejection, self.non_commit, self.uncertainty, self.deployment)
            if item is not None
        )
        if len(populated) != 1 or populated[0].operation_id != self.operation_id:
            raise ValueError("outcome must carry exactly one same-operation evidence variant")
        if self.kind is CreateOutcomeKind.COMMITTED:
            expected_reason = (
                OutcomeReasonCode.TRANSACTION_EVENT_BOUND
                if isinstance(self.deployment.binding_evidence, TransactionEventEvidence)
                else OutcomeReasonCode.EXACT_CHAIN_READ_BOUND
            )
            if self.reason_code is not expected_reason:
                raise ValueError("committed reason does not describe its binding evidence")


@dataclass(frozen=True)
class HandoffFailure:
    operation_id: str
    subject: DeploymentKey
    source: str
    evidence_digest: str
    observed_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey):
            raise ValueError("handoff failure requires an exact subject")
        _nonempty(self.source, "source")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")


@dataclass(frozen=True)
class CreatorRollbackRequest:
    operation_id: str
    subject: DeploymentKey
    request_id: str
    source: str
    evidence_digest: str
    requested_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey):
            raise ValueError("rollback request requires an exact subject")
        _nonempty(self.request_id, "request_id")
        _nonempty(self.source, "source")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.requested_at, "requested_at")


@dataclass(frozen=True)
class ExecutionClosure:
    """Complete terminal populations observed through two finalized trust paths.

    The adapter authenticates operator identities, establishes that the trust
    paths are not aliases, and determines the close transaction and finalized
    block heights. This sans-I/O boundary rejects a closure claim unless those
    decisions are explicit and internally consistent.
    """

    operation_id: str
    subject: DeploymentKey
    chain_id: str
    proof_mode: ExecutionClosureProofMode
    source_a: str
    source_b: str
    operator_identity_a: str
    operator_identity_b: str
    trust_path_a: str
    trust_path_b: str
    operator_independence_verified: bool
    source_a_height: int
    source_b_height: int
    common_finality_height: int
    close_transaction_height: int
    group_population_digest: str
    lease_population_digest: str
    evidence_digest: str
    observed_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey):
            raise ValueError("execution closure requires an exact subject")
        _nonempty(self.chain_id, "chain_id")
        if not isinstance(self.proof_mode, ExecutionClosureProofMode):
            raise ValueError("execution closure requires a typed proof mode")
        for field_name in (
            "source_a",
            "source_b",
            "operator_identity_a",
            "operator_identity_b",
            "trust_path_a",
            "trust_path_b",
        ):
            _nonempty(getattr(self, field_name), field_name)
        if (
            self.source_a == self.source_b
            or self.operator_identity_a == self.operator_identity_b
            or self.trust_path_a == self.trust_path_b
            or self.operator_independence_verified is not True
        ):
            raise ValueError("execution closure requires two independently operated trust paths")
        for field_name in (
            "source_a_height",
            "source_b_height",
            "common_finality_height",
            "close_transaction_height",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.proof_mode is not ExecutionClosureProofMode.EXACT_FINALIZED_HEIGHT or not (
            self.source_a_height == self.source_b_height == self.common_finality_height
        ):
            raise ValueError("execution closure requires two reads at one exact finalized height")
        if self.common_finality_height < self.close_transaction_height:
            raise ValueError("execution closure observations must be at or after the close height")
        _digest(self.group_population_digest, "group_population_digest")
        _digest(self.lease_population_digest, "lease_population_digest")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")


@dataclass(frozen=True)
class SettlementEvidence:
    operation_id: str
    subject: DeploymentKey
    state: SettlementState
    source_a: str
    source_b: str
    evidence_digest: str
    observed_at: int

    def __post_init__(self) -> None:
        _operation(self.operation_id)
        if not isinstance(self.subject, DeploymentKey) or not isinstance(
            self.state, SettlementState
        ):
            raise ValueError("settlement requires typed subject and state")
        _nonempty(self.source_a, "source_a")
        _nonempty(self.source_b, "source_b")
        if self.source_a == self.source_b:
            raise ValueError("settlement requires two independent sources")
        _digest(self.evidence_digest, "evidence_digest")
        _time(self.observed_at, "observed_at")


TransitionPayload: TypeAlias = (
    PreparedCreate
    | SubmissionEvidence
    | CreateOutcome
    | HandoffFailure
    | CreatorRollbackRequest
    | ExecutionClosure
    | SettlementEvidence
)


@dataclass(frozen=True)
class JournalEntry:
    revision: int
    operation_id: str
    state: JournalState
    payload: TransitionPayload
    recorded_at: int
    previous_entry_digest: str | None
    entry_digest: str = ""
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.revision) is not int or self.revision <= 0:
            raise ValueError("revision must be a positive integer")
        _operation(self.operation_id)
        if not isinstance(self.state, JournalState):
            raise ValueError("journal state must be typed")
        _time(self.recorded_at, "recorded_at")
        if self.previous_entry_digest is not None:
            _digest(self.previous_entry_digest, "previous_entry_digest")
        expected = _entry_digest(self)
        if not self.entry_digest:
            object.__setattr__(self, "entry_digest", expected)
        elif self.entry_digest != expected:
            raise TransitionError("entry fields were rewritten after their digest was assigned")


def _entry_digest(entry: JournalEntry) -> str:
    payload = _EntryDigestPayload(
        revision=entry.revision,
        operation_id=entry.operation_id,
        state=entry.state,
        payload=entry.payload,
        recorded_at=entry.recorded_at,
        previous_entry_digest=entry.previous_entry_digest,
        schema_version=entry.schema_version,
    )
    return canonical_journal_digest(payload)


@dataclass(frozen=True)
class _EntryDigestPayload:
    revision: int
    operation_id: str
    state: JournalState
    payload: TransitionPayload
    recorded_at: int
    previous_entry_digest: str | None
    schema_version: int


_PAYLOAD_TYPES = {
    JournalState.PREPARED: PreparedCreate,
    JournalState.SUBMITTED: SubmissionEvidence,
    JournalState.REJECTED_BEFORE_SEND: CreateOutcome,
    JournalState.FALLBACK_SAFE: CreateOutcome,
    JournalState.CREATE_OUTCOME_UNKNOWN: CreateOutcome,
    JournalState.CREATED: CreateOutcome,
    JournalState.HANDOFF_FAILED: HandoffFailure,
    JournalState.CREATOR_ROLLBACK_REQUESTED: CreatorRollbackRequest,
    JournalState.EXECUTION_CLOSED: ExecutionClosure,
    JournalState.SETTLEMENT: SettlementEvidence,
}

_NEXT_STATES = {
    JournalState.PREPARED: {
        JournalState.SUBMITTED,
        JournalState.REJECTED_BEFORE_SEND,
        JournalState.CREATE_OUTCOME_UNKNOWN,
    },
    JournalState.SUBMITTED: {
        JournalState.CREATE_OUTCOME_UNKNOWN,
        JournalState.CREATED,
        JournalState.FALLBACK_SAFE,
    },
    JournalState.REJECTED_BEFORE_SEND: {JournalState.FALLBACK_SAFE},
    JournalState.CREATE_OUTCOME_UNKNOWN: {
        JournalState.CREATED,
        JournalState.FALLBACK_SAFE,
    },
    JournalState.CREATED: {JournalState.HANDOFF_FAILED, JournalState.EXECUTION_CLOSED},
    JournalState.HANDOFF_FAILED: {JournalState.CREATOR_ROLLBACK_REQUESTED},
    JournalState.CREATOR_ROLLBACK_REQUESTED: {JournalState.EXECUTION_CLOSED},
    JournalState.EXECUTION_CLOSED: {JournalState.SETTLEMENT},
    JournalState.SETTLEMENT: {JournalState.SETTLEMENT},
    JournalState.FALLBACK_SAFE: set(),
}

_OUTCOME_STATES = {
    JournalState.REJECTED_BEFORE_SEND: CreateOutcomeKind.REJECTED_BEFORE_SEND,
    JournalState.FALLBACK_SAFE: CreateOutcomeKind.FALLBACK_SAFE,
    JournalState.CREATE_OUTCOME_UNKNOWN: CreateOutcomeKind.UNKNOWN,
    JournalState.CREATED: CreateOutcomeKind.COMMITTED,
}


def _operation_entries(
    entries: tuple[JournalEntry, ...], operation_id: str
) -> tuple[JournalEntry, ...]:
    return tuple(entry for entry in entries if entry.operation_id == operation_id)


def _allocation_key(prepared: PreparedCreate) -> tuple[object, ...]:
    return (
        prepared.owner_candidate.owner,
        prepared.producer.repository_id,
        prepared.producer.repository_owner_id,
        prepared.producer.subject,
        prepared.lifecycle,
        prepared.operation_ordinal,
    )


def _bound(entries: tuple[JournalEntry, ...], operation_id: str) -> BoundDeployment | None:
    for entry in _operation_entries(entries, operation_id):
        if entry.state is JournalState.CREATED:
            return entry.payload.deployment
    return None


def _payload_observed_at(payload: TransitionPayload) -> int:
    if isinstance(payload, PreparedCreate):
        return payload.prepared_at
    if isinstance(payload, SubmissionEvidence):
        return payload.submitted_at
    if isinstance(payload, CreateOutcome):
        evidence = payload.rejection or payload.non_commit or payload.uncertainty
        if evidence is not None:
            return evidence.observed_at
        if isinstance(payload.deployment.binding_evidence, ExactChainReadEvidence):
            return payload.deployment.binding_evidence.observed_at
        return 0
    if isinstance(payload, CreatorRollbackRequest):
        return payload.requested_at
    return payload.observed_at


def _validate_entry(entries: tuple[JournalEntry, ...], entry: JournalEntry) -> None:
    if entry.revision != len(entries) + 1:
        raise TransitionError("journal revision is not the next append-only revision")
    expected_previous = entries[-1].entry_digest if entries else None
    if entry.previous_entry_digest != expected_previous:
        raise TransitionError("journal entry does not extend the current digest chain")
    if type(entry.payload) is not _PAYLOAD_TYPES[entry.state]:
        raise TransitionError("journal state does not match its typed payload")
    if entry.payload.operation_id != entry.operation_id:
        raise TransitionError("journal entry and payload operation IDs disagree")
    if entry.recorded_at < _payload_observed_at(entry.payload):
        raise TransitionError("journal entry predates its evidence")
    if entries and entry.recorded_at < entries[-1].recorded_at:
        raise TransitionError("journal append time moved backwards")
    operation_entries = _operation_entries(entries, entry.operation_id)
    if not operation_entries:
        if entry.state is not JournalState.PREPARED:
            raise TransitionError("the first operation transition must be prepared")
        for prior in entries:
            if prior.state is not JournalState.PREPARED:
                continue
            if prior.payload.operation_id == entry.payload.operation_id:
                raise TransitionError("duplicate operation ID")
            if _allocation_key(prior.payload) == _allocation_key(entry.payload):
                raise TransitionError("duplicate operation ordinal for lifecycle identity")
        return
    if entry.state is JournalState.PREPARED:
        raise TransitionError("duplicate operation ID")
    previous = operation_entries[-1]
    if entry.state not in _NEXT_STATES[previous.state]:
        raise TransitionError(f"invalid transition {previous.state.value} -> {entry.state.value}")
    prepared = operation_entries[0].payload
    if entry.recorded_at < previous.recorded_at or entry.recorded_at < prepared.prepared_at:
        raise TransitionError("transition time moved backwards")
    if isinstance(entry.payload, SubmissionEvidence) and (
        entry.payload.backend != prepared.owner_candidate.backend
        or entry.payload.submitted_request_digest != prepared.request_digest
        or entry.payload.submitted_at < prepared.prepared_at
    ):
        raise TransitionError("submission evidence disagrees with prepared request or backend")
    if isinstance(entry.payload, CreateOutcome):
        expected_kind = _OUTCOME_STATES[entry.state]
        if entry.payload.kind is not expected_kind:
            raise TransitionError("outcome kind disagrees with journal state")
        if entry.payload.rejection is not None and (
            entry.payload.rejection.backend != prepared.owner_candidate.backend
        ):
            raise TransitionError("rejection backend disagrees with prepared backend")
        if entry.payload.non_commit is not None:
            proof = entry.payload.non_commit
            if (
                proof.backend != prepared.owner_candidate.backend
                or proof.exact_owner != prepared.owner_candidate.owner
                or proof.group_population_digest != prepared.group_population_digest
            ):
                raise TransitionError("non-commit proof disagrees with prepared identity")
            submissions = [
                item.payload for item in operation_entries if item.state is JournalState.SUBMITTED
            ]
            if submissions:
                submission = submissions[-1]
                if (
                    proof.transaction_hash != submission.transaction_hash
                    or proof.submission_token != submission.submission_token
                ):
                    raise TransitionError("non-commit proof is bound to another submission")
        if entry.payload.deployment is not None:
            deployment = entry.payload.deployment
            if (
                deployment.subject.owner != prepared.owner_candidate.owner
                or deployment.groups != prepared.groups
                or deployment.group_population_digest != prepared.group_population_digest
            ):
                raise TransitionError("bound deployment disagrees with prepared owner or groups")
            submissions = [
                item.payload for item in operation_entries if item.state is JournalState.SUBMITTED
            ]
            if isinstance(deployment.binding_evidence, TransactionEventEvidence) and (
                not submissions
                or submissions[-1].transaction_hash != deployment.binding_evidence.transaction_hash
            ):
                raise TransitionError("transaction event is not bound to submitted transaction")
            for prior in entries:
                prior_bound = _bound(entries, prior.operation_id)
                if prior_bound is not None and prior_bound.subject == deployment.subject:
                    raise TransitionError("one deployment cannot be bound to two operations")
    subject = _bound(entries, entry.operation_id)
    if isinstance(
        entry.payload,
        (HandoffFailure, CreatorRollbackRequest, ExecutionClosure, SettlementEvidence),
    ) and (subject is None or entry.payload.subject != subject.subject):
        raise TransitionError("transition changed or lacks the bound deployment identity")
    if isinstance(entry.payload, ExecutionClosure) and (
        entry.payload.group_population_digest != prepared.group_population_digest
    ):
        raise TransitionError("execution closure changed the prepared group population")
    if isinstance(entry.payload, SettlementEvidence) and previous.state is JournalState.SETTLEMENT:
        order = {
            SettlementState.UNMEASURED: 0,
            SettlementState.UNSETTLED: 1,
            SettlementState.SETTLED: 2,
        }
        if order[entry.payload.state] <= order[previous.payload.state]:
            raise TransitionError("settlement state must advance monotonically")


@dataclass(frozen=True)
class CreateJournal:
    entries: tuple[JournalEntry, ...] = ()
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.entries) is not tuple:
            raise TransitionError("journal entries must be an immutable tuple")
        checked: tuple[JournalEntry, ...] = ()
        for entry in self.entries:
            if not isinstance(entry, JournalEntry):
                raise TransitionError("journal entries must be typed")
            _validate_entry(checked, entry)
            checked += (entry,)

    @property
    def revision(self) -> int:
        return len(self.entries)

    @property
    def digest(self) -> str | None:
        return self.entries[-1].entry_digest if self.entries else None

    def append(
        self,
        state: JournalState,
        payload: TransitionPayload,
        *,
        recorded_at: int,
    ) -> CreateJournal:
        """Return a new journal after validating the only permitted append path."""

        if not isinstance(state, JournalState) or type(payload) is not _PAYLOAD_TYPES.get(state):
            raise TransitionError("journal state does not match its typed payload")
        entry = JournalEntry(
            revision=self.revision + 1,
            operation_id=payload.operation_id,
            state=state,
            payload=payload,
            recorded_at=recorded_at,
            previous_entry_digest=self.digest,
        )
        return CreateJournal(self.entries + (entry,))


def append_transition(
    journal: CreateJournal,
    state: JournalState,
    payload: TransitionPayload,
    *,
    recorded_at: int,
) -> CreateJournal:
    """Functional spelling of :meth:`CreateJournal.append` for adapters."""

    if not isinstance(journal, CreateJournal):
        raise TransitionError("append requires a validated CreateJournal")
    return journal.append(state, payload, recorded_at=recorded_at)


__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "BackendIdentity",
    "BackendKind",
    "BoundDeployment",
    "CreateJournal",
    "CreateOutcome",
    "CreateOutcomeKind",
    "CreatorRollbackRequest",
    "ExactChainReadEvidence",
    "ExecutionClosure",
    "ExecutionClosureProofMode",
    "HandoffFailure",
    "JournalEntry",
    "JournalState",
    "LifecycleIdentity",
    "NonCommitEvidence",
    "OutcomeReasonCode",
    "OwnerCandidate",
    "OwnerEvidenceKind",
    "PreparedCreate",
    "PreparedGroup",
    "ProducerProvenance",
    "RejectionEvidence",
    "SettlementEvidence",
    "SettlementState",
    "SubmissionEvidence",
    "TransactionEventEvidence",
    "TransitionError",
    "UncertaintyEvidence",
    "append_transition",
    "canonical_group_population_digest",
    "canonical_journal_bytes",
    "canonical_journal_digest",
    "canonical_payload_digest",
]
