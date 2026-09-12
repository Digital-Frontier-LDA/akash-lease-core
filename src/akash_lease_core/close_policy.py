"""Pure, sans-I/O authorization policy for Akash deployment retirement.

Inputs are claims or adapter-produced evidence. This module performs no OIDC,
signature, chain, database, clock, or destructive I/O. ``CreationAttestation``
is neutral claim data; only a separately bound ``AttestationVerification`` can
authenticate it for a shared signer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum

from .chain_identity import DeploymentKey, is_canonical_akash_owner
from .workload_identity import Identity, Population, PopulationCompleteness

CLOSE_POLICY_VERSION = 2


class CloseIntent(str, Enum):
    CI_CLEANUP = "ci_cleanup"
    STAGING_RETIREMENT = "staging_retirement"
    CREATOR_ROLLBACK = "creator_rollback"
    PRODUCTION_RETIREMENT = "production_retirement"


class CloseDisposition(str, Enum):
    ALLOW = "allow"
    HOLD = "hold"
    DENY = "deny"


class CloseReasonCode(str, Enum):
    """Stable machine reasons for every close-policy outcome.

    Values are grouped by the policy stage which stopped evaluation.  Human
    diagnostics live in :attr:`CloseDecision.message` and may change without
    changing adapter behaviour.
    """

    CLASSIFICATION_INCOMPLETE = "classification.incomplete"
    CLASSIFICATION_HELD = "classification.held"
    CLASSIFICATION_COUNT_MISMATCH = "classification.count_mismatch"
    CLASSIFICATION_LIFECYCLE_MIXED = "classification.lifecycle_mixed"
    CANDIDATE_INCOMPLETE_OR_NON_UNIQUE = "candidate.incomplete_or_non_unique"
    CLASS_POLICY_FORBIDS_INTENT = "class_policy.forbids_intent"
    PRODUCER_UNAUTHENTICATED = "producer_authentication.missing"
    PRODUCER_SHARED_EVIDENCE_INVALID = "producer_authentication.shared_invalid"
    PRODUCER_ISOLATED_EVIDENCE_INVALID = "producer_authentication.isolated_invalid"
    CHAIN_EVIDENCE_MISSING = "chain_evidence.missing"
    CHAIN_EVIDENCE_WRONG_POPULATION = "chain_evidence.wrong_population"
    CHAIN_EVIDENCE_TRUST_PATHS_INCOMPLETE = "chain_evidence.trust_paths_incomplete"
    CHAIN_EVIDENCE_DIGEST_OR_INTERVAL_INCOMPLETE = "chain_evidence.digest_or_interval_incomplete"
    CHAIN_POPULATION_INCOMPLETE_OR_MISMATCHED = "chain_evidence.population_mismatch"
    CHAIN_LEASE_POPULATION_INCOMPLETE = "chain_evidence.lease_population_incomplete"
    CHAIN_SOURCES_DISAGREE = "chain_evidence.sources_disagree"
    AUTHORITY_MISSING = "lifecycle_authority.missing"
    AUTHORITY_EVIDENCE_INCOMPLETE = "lifecycle_authority.evidence_incomplete"
    AUTHORITY_REPLAYED = "lifecycle_authority.replayed"
    AUTHORITY_INVALID = "lifecycle_authority.invalid"
    AUTHORITY_WRONG_TYPE = "lifecycle_authority.wrong_type"
    AUTHORITY_IDENTITY_DISAGREEMENT = "lifecycle_authority.identity_disagreement"
    AUTHORIZATION_SUCCEEDED = "authorization.succeeded"


_CLOSE_REASON_DISPOSITIONS = {
    CloseReasonCode.CLASSIFICATION_INCOMPLETE: CloseDisposition.HOLD,
    CloseReasonCode.CLASSIFICATION_HELD: CloseDisposition.HOLD,
    CloseReasonCode.CLASSIFICATION_COUNT_MISMATCH: CloseDisposition.HOLD,
    CloseReasonCode.CLASSIFICATION_LIFECYCLE_MIXED: CloseDisposition.HOLD,
    CloseReasonCode.CANDIDATE_INCOMPLETE_OR_NON_UNIQUE: CloseDisposition.HOLD,
    CloseReasonCode.CLASS_POLICY_FORBIDS_INTENT: CloseDisposition.DENY,
    CloseReasonCode.PRODUCER_UNAUTHENTICATED: CloseDisposition.HOLD,
    CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID: CloseDisposition.HOLD,
    CloseReasonCode.PRODUCER_ISOLATED_EVIDENCE_INVALID: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_EVIDENCE_MISSING: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_EVIDENCE_WRONG_POPULATION: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_EVIDENCE_TRUST_PATHS_INCOMPLETE: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_EVIDENCE_DIGEST_OR_INTERVAL_INCOMPLETE: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_POPULATION_INCOMPLETE_OR_MISMATCHED: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_LEASE_POPULATION_INCOMPLETE: CloseDisposition.HOLD,
    CloseReasonCode.CHAIN_SOURCES_DISAGREE: CloseDisposition.HOLD,
    CloseReasonCode.AUTHORITY_MISSING: CloseDisposition.HOLD,
    CloseReasonCode.AUTHORITY_EVIDENCE_INCOMPLETE: CloseDisposition.HOLD,
    CloseReasonCode.AUTHORITY_REPLAYED: CloseDisposition.HOLD,
    CloseReasonCode.AUTHORITY_INVALID: CloseDisposition.DENY,
    CloseReasonCode.AUTHORITY_WRONG_TYPE: CloseDisposition.DENY,
    CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT: CloseDisposition.DENY,
    CloseReasonCode.AUTHORIZATION_SUCCEEDED: CloseDisposition.ALLOW,
}


class SourceAgreement(str, Enum):
    AGREEING = "agreeing"
    DISAGREEING = "disagreeing"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class CandidateCompleteness(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class RunState(str, Enum):
    TERMINAL = "terminal"
    LIVE = "live"
    UNKNOWN = "unknown"


class ConsumerState(str, Enum):
    FINISHED = "finished"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class UniquenessStatus(str, Enum):
    UNIQUE = "unique"
    REUSED = "reused"
    UNKNOWN = "unknown"


class ProductionApprovalMode(str, Enum):
    GITHUB_ENVIRONMENT = "github_environment"
    EXTERNAL = "external"


class ProductionAuthorizationAction(str, Enum):
    PRODUCTION_RETIREMENT = "production_retirement"
    OTHER = "other"


class EnvironmentControlStatus(str, Enum):
    VERIFIED_ENABLED = "verified_enabled"
    VERIFIED_DISABLED = "verified_disabled"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class PrincipalKind(str, Enum):
    HUMAN = "human"
    MACHINE = "machine"


class MembershipStatus(str, Enum):
    VERIFIED_MEMBER = "verified_member"
    VERIFIED_NON_MEMBER = "verified_non_member"
    UNKNOWN = "unknown"


class BypassUseStatus(str, Enum):
    UNUSED = "unused"
    USED = "used"
    UNKNOWN = "unknown"


class CreationBindingStatus(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CreationBindingMode(str, Enum):
    TRANSACTION_EVENT = "transaction_event"
    EXACT_CHAIN_READBACK = "exact_chain_readback"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthorityValidation:
    """Typed result for lifecycle-authority validation."""

    reason_code: CloseReasonCode
    message: str
    evidence_gap: bool = False


@dataclass(frozen=True)
class _Reason:
    reason_code: CloseReasonCode
    message: str


def _authority_hold(
    message: str,
    reason_code: CloseReasonCode = CloseReasonCode.AUTHORITY_EVIDENCE_INCOMPLETE,
) -> AuthorityValidation:
    return AuthorityValidation(reason_code, message, True)


def _authority_deny(message: str) -> AuthorityValidation:
    return AuthorityValidation(CloseReasonCode.AUTHORITY_INVALID, message)


class HandoffState(str, Enum):
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"


class ChainProofMode(str, Enum):
    EXACT_FINALIZED_HEIGHT = "exact_finalized_height"
    LATEST_UNFINALIZED = "latest_unfinalized"


class PreCloseDeploymentState(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class LeasePopulationCompleteness(str, Enum):
    COMPLETE = "complete"
    EXACT_NEVER_LEASED = "exact_never_leased"
    INCOMPLETE = "incomplete"


EMPTY_POPULATION_DIGEST = hashlib.sha256(b"[]").hexdigest()


@dataclass(frozen=True)
class CandidatePopulation:
    subject: DeploymentKey
    group_identity_digest: str
    operation_id: str
    operation_ordinal: int
    count: int
    completeness: CandidateCompleteness
    prepared_operation_key: str
    uniqueness_status: UniquenessStatus
    evidence_digest: str
    creation_authorization_reference: str | None = None


@dataclass(frozen=True)
class ChainEvidence:
    subject: DeploymentKey
    group_identity_digest: str
    evidence_digest: str
    chain_id: str
    source_a: str
    source_b: str
    trust_path_a: str
    trust_path_b: str
    source_a_height: int
    source_b_height: int
    common_finality_height: int
    proof_mode: ChainProofMode
    deployment_count: int
    deployment_population_digest: str
    deployment_state: PreCloseDeploymentState
    deployment_state_digest: str
    group_count: int
    group_population_digest: str
    group_state_digest: str
    lease_count: int
    lease_population_digest: str
    lease_state_digest: str
    lease_completeness: LeasePopulationCompleteness
    observed_at: int
    valid_until: int
    agreement: SourceAgreement


@dataclass(frozen=True)
class CIAuthority:
    subject: DeploymentKey
    repository: str
    run: int
    attempt: int
    run_state: RunState
    consumer_state: ConsumerState
    source: str
    observation_digest: str
    observed_at: int
    valid_until: int


@dataclass(frozen=True)
class StagingRetirementAuthority:
    subject: DeploymentKey
    repository: str
    release: str
    authorization_reference: str
    source: str
    observation_digest: str
    observed_at: int
    valid_until: int


@dataclass(frozen=True)
class ProductionPrincipal:
    principal: str
    kind: PrincipalKind
    authentication_status: VerificationStatus
    admin_membership: MembershipStatus
    approval_role_membership: MembershipStatus
    evidence_source: str
    evidence_digest: str


@dataclass(frozen=True)
class ProductionBypassEvidence:
    configuration_status: EnvironmentControlStatus
    use_status: BypassUseStatus
    observation_verification_status: VerificationStatus
    enabled_bypass_policy_status: VerificationStatus
    observation_source: str
    observation_digest: str
    actor_principal: str | None


@dataclass(frozen=True)
class ProductionRetirementAuthority:
    subject: DeploymentKey
    repository: str
    release: str
    environment: str
    authorization_id: str
    authorization_reference: str
    requester: ProductionPrincipal
    approver: ProductionPrincipal
    executor: ProductionPrincipal
    action: ProductionAuthorizationAction
    approval_mode: ProductionApprovalMode
    approval_verification_status: VerificationStatus
    single_use_verification: VerificationStatus
    authorization_uniqueness_status: UniquenessStatus
    prevent_self_review_status: EnvironmentControlStatus
    bypass: ProductionBypassEvidence | None
    policy_reference: str
    approval_verification_source: str
    approval_verification_digest: str
    verified_authorization_id: str
    verified_environment: str
    verified_release: str
    verified_subject: DeploymentKey
    verified_action: ProductionAuthorizationAction
    single_use_authorization_id: str
    unique_authorization_id: str
    issued_at: int
    observed_at: int
    valid_until: int
    workflow_ref: str | None
    workflow_sha: str | None
    ref: str | None
    verified_workflow_ref: str | None
    verified_workflow_sha: str | None
    verified_ref: str | None


@dataclass(frozen=True)
class CreatorRollbackAuthority:
    """One-operation capability established before the create effect."""

    subject: DeploymentKey
    operation_id: str
    repository: str
    workload_class: str
    run: int | None
    run_attempt: int | None
    release: str | None
    expires: int | None
    creation_authorization_reference: str | None
    backend: str
    signer_owner: str
    group_identity_digest: str
    capability_reference: str
    binding_status: CreationBindingStatus
    binding_mode: CreationBindingMode
    binding_source: str
    binding_evidence_digest: str
    prepared_at: int
    valid_until: int
    rollback_trigger: RollbackTriggerEvidence


@dataclass(frozen=True)
class RollbackTriggerEvidence:
    """Evidence that handoff failed and compensation is currently required."""

    handoff_state: HandoffState
    source: str
    evidence_digest: str
    observed_at: int
    valid_until: int


LifecycleAuthority = (
    CIAuthority
    | StagingRetirementAuthority
    | ProductionRetirementAuthority
    | CreatorRollbackAuthority
)


@dataclass(frozen=True)
class IsolatedSignerEvidence:
    signer_owner: str
    repository: str
    workload_class: str
    operation_id: str
    environment: str | None
    trust_domain: str
    source: str
    evidence_digest: str
    observed_at: int
    valid_until: int


@dataclass(frozen=True)
class CreationAttestation:
    """Neutral creation claims; construction does not verify them."""

    schema_version: int
    audience: str
    issuer: str
    operation_id: str
    subject: DeploymentKey
    group_identity_digest: str
    producer_repository: str
    workload_class: str
    repository_id: str
    repository_owner_id: str
    ref: str
    workflow_ref: str
    workflow_sha: str
    producer_oidc_issuer: str
    producer_oidc_audience: str
    producer_oidc_subject: str
    producer_oidc_jti: str
    issued_at: int
    job_workflow_ref: str | None = None
    job_workflow_sha: str | None = None
    run: int | None = None
    run_attempt: int | None = None
    release: str | None = None
    authorization_reference: str | None = None
    environment: str | None = None


@dataclass(frozen=True)
class AttestationVerification:
    """Adapter result bound to one attestation and one pinned trust root."""

    attestation_digest: str
    trust_root: str
    verified_issuer: str
    verified_audience: str
    verified_producer_oidc_issuer: str
    verified_producer_oidc_audience: str
    verified_producer_oidc_subject: str
    verified_producer_oidc_jti: str
    signature_status: VerificationStatus
    uniqueness_status: UniquenessStatus
    producer_jti_uniqueness_status: UniquenessStatus
    observed_at: int
    valid_until: int


@dataclass(frozen=True)
class SharedSignerEvidence:
    attestation: CreationAttestation
    verification: AttestationVerification


ProducerAuthentication = IsolatedSignerEvidence | SharedSignerEvidence


@dataclass(frozen=True)
class CloseDecision:
    disposition: CloseDisposition
    intent: CloseIntent
    subject: DeploymentKey
    reason_code: CloseReasonCode
    message: str
    workload_class: str | None = None
    observed_groups: int = 0
    matching_candidate_count: int = 0
    group_identity_digest: str | None = None
    authority_principal: str | None = None
    authority_digest: str | None = None
    preclose_evidence_digest: str | None = None
    evidence_observed_at: int | None = None
    evidence_valid_until: int | None = None
    evaluated_at: int = 0
    operation_id: str | None = None
    candidate_evidence_digest: str | None = None
    prepared_operation_key: str | None = None
    chain_id: str | None = None
    proof_mode: ChainProofMode | None = None
    finalized_height: int | None = None
    policy_version: int = CLOSE_POLICY_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.disposition, CloseDisposition)
            or not isinstance(self.intent, CloseIntent)
            or not isinstance(self.subject, DeploymentKey)
            or not isinstance(self.reason_code, CloseReasonCode)
            or not _nonempty(self.message)
        ):
            raise ValueError("close decision requires typed identity, disposition, and reason")
        if type(self.policy_version) is not int or self.policy_version != CLOSE_POLICY_VERSION:
            raise ValueError("close decision policy version does not match this contract")
        expected_disposition = _CLOSE_REASON_DISPOSITIONS.get(self.reason_code)
        if expected_disposition is not self.disposition:
            raise ValueError("close reason code and disposition must agree")

    @property
    def allowed(self) -> bool:
        return self.disposition is CloseDisposition.ALLOW

    @property
    def reason(self) -> str:
        """Compatibility alias for the human diagnostic; branch on ``reason_code``."""

        return self.message


def _digest(value: object) -> str:
    if not is_dataclass(value) or isinstance(value, type):
        raise ValueError("canonical digest requires typed dataclass evidence")
    encoded = json.dumps(asdict(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _classified(population: object) -> tuple[Identity | None, _Reason | None]:
    if not isinstance(population, Population):
        raise ValueError("an explicit workload Population is required")
    if population.completeness is not PopulationCompleteness.COMPLETE:
        return None, _Reason(
            CloseReasonCode.CLASSIFICATION_INCOMPLETE,
            "group population is incomplete or unverified",
        )
    if population.held:
        return None, _Reason(
            CloseReasonCode.CLASSIFICATION_HELD,
            population.reason or "workload population is held",
        )
    if (
        not population.identities
        or population.parsed_count != population.observed_count
        or len(population.identities) != population.observed_count
    ):
        return None, _Reason(
            CloseReasonCode.CLASSIFICATION_COUNT_MISMATCH,
            "classified population counts disagree",
        )
    first = population.identities[0]
    if any(item.lifecycle != first.lifecycle for item in population.identities):
        return None, _Reason(
            CloseReasonCode.CLASSIFICATION_LIFECYCLE_MIXED,
            "mixed workload lifecycle population",
        )
    return first, None


def canonical_group_identity_digest(population: Population) -> str:
    identity, reason = _classified(population)
    if identity is None:
        if reason is None:  # pragma: no cover - closed internal result type
            raise AssertionError("classification failure requires a reason")
        raise ValueError(reason.message)
    payload = [asdict(item) for item in sorted(population.identities, key=lambda item: item.group)]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def canonical_attestation_digest(attestation: CreationAttestation) -> str:
    if not isinstance(attestation, CreationAttestation):
        raise ValueError("typed creation attestation claims are required")
    return _digest(attestation)


def canonical_prepared_operation_key(
    *,
    signer_owner: str,
    producer_repository: str,
    workload_class: str,
    operation_id: str,
    operation_ordinal: int,
    prepared_group_identity_digest: str,
    run: int | None = None,
    run_attempt: int | None = None,
    release: str | None = None,
) -> str:
    """Bind journal allocation using facts available before creation."""

    if not is_canonical_akash_owner(signer_owner) or not _nonempty(
        producer_repository,
        workload_class,
        operation_id,
        prepared_group_identity_digest,
    ):
        raise ValueError("canonical signer and prepared operation identity are required")
    if type(operation_ordinal) is not int or operation_ordinal <= 0:
        raise ValueError("operation ordinal must be a canonical positive integer")
    if workload_class.startswith("ci-"):
        if type(run) is not int or type(run_attempt) is not int or release is not None:
            raise ValueError("CI operation key requires run and run_attempt")
    elif workload_class in {"staging-payload", "prod-payload"}:
        if run is not None or run_attempt is not None or not _nonempty(release):
            raise ValueError("payload operation key requires a release")
    else:
        raise ValueError("unknown workload class")
    encoded = json.dumps(
        {
            "signer_owner": signer_owner,
            "producer_repository": producer_repository,
            "workload_class": workload_class,
            "operation_id": operation_id,
            "operation_ordinal": operation_ordinal,
            "prepared_group_identity_digest": prepared_group_identity_digest,
            "run": run,
            "run_attempt": run_attempt,
            "release": release,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def canonical_deployment_population_digest(subject: DeploymentKey) -> str:
    """Digest the exact singleton deployment population used by a close."""

    if not isinstance(subject, DeploymentKey):
        raise ValueError("an exact DeploymentKey subject is required")
    encoded = json.dumps([asdict(subject)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _nonempty(*values: object) -> bool:
    return all(isinstance(value, str) and bool(value.strip()) for value in values)


def _window(start: object, evaluated_at: object, end: object) -> bool:
    return (
        type(start) is int
        and type(evaluated_at) is int
        and type(end) is int
        and start > 0
        and start <= evaluated_at <= end
    )


def _authority_context(authority: LifecycleAuthority | None) -> tuple[str | None, str | None]:
    if authority is None:
        return None, None
    if isinstance(authority, CIAuthority):
        principal = f"ci:{authority.repository}:{authority.run}:{authority.attempt}"
    elif isinstance(authority, StagingRetirementAuthority):
        principal = authority.authorization_reference
    elif isinstance(authority, ProductionRetirementAuthority):
        principal = authority.executor.principal
    elif isinstance(authority, CreatorRollbackAuthority):
        principal = f"creator:{authority.backend}:{authority.operation_id}"
    else:
        raise ValueError("lifecycle authority must use a typed authority variant")
    return principal, _digest(authority)


def _decision(
    disposition: CloseDisposition,
    intent: CloseIntent,
    subject: DeploymentKey,
    population: Population,
    candidates: CandidatePopulation,
    reason_code: CloseReasonCode,
    message: str,
    *,
    evaluated_at: int,
    identity: Identity | None = None,
    authority: LifecycleAuthority | None = None,
    chain: ChainEvidence | None = None,
) -> CloseDecision:
    principal, authority_digest = _authority_context(authority)
    return CloseDecision(
        disposition,
        intent,
        subject,
        reason_code,
        message,
        workload_class=identity.workload_class if identity else None,
        observed_groups=population.observed_count,
        matching_candidate_count=candidates.count,
        group_identity_digest=(canonical_group_identity_digest(population) if identity else None),
        authority_principal=principal,
        authority_digest=authority_digest,
        preclose_evidence_digest=chain.evidence_digest if chain else None,
        evidence_observed_at=chain.observed_at if chain else None,
        evidence_valid_until=chain.valid_until if chain else None,
        evaluated_at=evaluated_at,
        operation_id=candidates.operation_id,
        candidate_evidence_digest=candidates.evidence_digest,
        prepared_operation_key=candidates.prepared_operation_key,
        chain_id=chain.chain_id if chain else None,
        proof_mode=chain.proof_mode if chain else None,
        finalized_height=chain.common_finality_height if chain else None,
    )


def _shared_reason(
    evidence: SharedSignerEvidence,
    subject: DeploymentKey,
    population: Population,
    identity: Identity,
    candidates: CandidatePopulation,
    evaluated_at: int,
) -> _Reason | None:
    if not isinstance(evidence.attestation, CreationAttestation) or not isinstance(
        evidence.verification, AttestationVerification
    ):
        raise ValueError("shared signer evidence requires typed claims and verification")
    claim, check = evidence.attestation, evidence.verification
    expected = {
        "subject": subject,
        "group_identity_digest": canonical_group_identity_digest(population),
        "producer_repository": identity.owner,
        "workload_class": identity.workload_class,
        "run": identity.run,
        "run_attempt": identity.attempt,
        "release": identity.release,
        "operation_id": candidates.operation_id,
    }
    for field, value in expected.items():
        if getattr(claim, field) != value:
            return _Reason(
                CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
                f"creation attestation {field} does not match observed deployment",
            )
    if type(claim.schema_version) is not int or claim.schema_version != 1:
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "unsupported creation attestation schema version",
        )
    if not _nonempty(
        claim.audience,
        claim.issuer,
        claim.operation_id,
        claim.repository_id,
        claim.repository_owner_id,
        claim.ref,
        claim.workflow_ref,
        claim.producer_oidc_issuer,
        claim.producer_oidc_audience,
        claim.producer_oidc_subject,
        claim.producer_oidc_jti,
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "creation attestation is missing provenance claims",
        )
    if (
        re.fullmatch(r"[1-9][0-9]*", claim.repository_id) is None
        or re.fullmatch(r"[1-9][0-9]*", claim.repository_owner_id) is None
        or re.fullmatch(r"[0-9a-f]{40}", claim.workflow_sha) is None
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "creation attestation provenance is not immutable",
        )
    job_pair_present = claim.job_workflow_ref is not None or claim.job_workflow_sha is not None
    if job_pair_present and (
        not _nonempty(claim.job_workflow_ref)
        or not isinstance(claim.job_workflow_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", claim.job_workflow_sha) is None
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "reusable workflow ref and sha must be present together",
        )
    if type(claim.issued_at) is not int or claim.issued_at <= 0:
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "creation attestation has no canonical issued-at time",
        )
    if identity.run is not None and (
        type(claim.run) is not int or type(claim.run_attempt) is not int
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "creation attestation CI lifecycle numbers are not canonical integers",
        )
    if identity.workload_class in {"staging-payload", "prod-payload"} and not _nonempty(
        claim.authorization_reference, claim.environment
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "payload creation attestation lacks its authorization reference",
        )
    if check.attestation_digest != canonical_attestation_digest(claim):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "verification is bound to another attestation",
        )
    if not _nonempty(
        check.trust_root,
        check.verified_issuer,
        check.verified_audience,
        check.verified_producer_oidc_issuer,
        check.verified_producer_oidc_audience,
        check.verified_producer_oidc_subject,
        check.verified_producer_oidc_jti,
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "attestation verification lacks its trust binding",
        )
    if check.verified_issuer != claim.issuer or check.verified_audience != claim.audience:
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "attestation verifier result disagrees with the claims",
        )
    if (
        check.verified_producer_oidc_issuer,
        check.verified_producer_oidc_audience,
        check.verified_producer_oidc_subject,
        check.verified_producer_oidc_jti,
    ) != (
        claim.producer_oidc_issuer,
        claim.producer_oidc_audience,
        claim.producer_oidc_subject,
        claim.producer_oidc_jti,
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "producer OIDC verifier result disagrees with broker claims",
        )
    if check.signature_status is not VerificationStatus.VERIFIED:
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "attestation signature is failed or unverified",
        )
    if check.uniqueness_status is not UniquenessStatus.UNIQUE:
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "creation operation id is reused or unverified",
        )
    if check.producer_jti_uniqueness_status is not UniquenessStatus.UNIQUE:
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "producer OIDC jti is reused or unverified",
        )
    if claim.issued_at > check.observed_at or not _window(
        check.observed_at, evaluated_at, check.valid_until
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_SHARED_EVIDENCE_INVALID,
            "attestation verification is not valid at evaluation time",
        )
    return None


def _isolated_reason(
    evidence: IsolatedSignerEvidence,
    subject: DeploymentKey,
    identity: Identity,
    candidates: CandidatePopulation,
    evaluated_at: int,
) -> _Reason | None:
    if (evidence.signer_owner, evidence.repository, evidence.workload_class) != (
        subject.owner,
        identity.owner,
        identity.workload_class,
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_ISOLATED_EVIDENCE_INVALID,
            "isolated signer evidence does not match deployment identity",
        )
    if evidence.operation_id != candidates.operation_id:
        return _Reason(
            CloseReasonCode.PRODUCER_ISOLATED_EVIDENCE_INVALID,
            "isolated signer evidence does not match creation operation",
        )
    if identity.workload_class in {"staging-payload", "prod-payload"} and not _nonempty(
        evidence.environment
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_ISOLATED_EVIDENCE_INVALID,
            "payload signer evidence lacks environment binding",
        )
    if not _nonempty(
        evidence.operation_id, evidence.trust_domain, evidence.source, evidence.evidence_digest
    ):
        return _Reason(
            CloseReasonCode.PRODUCER_ISOLATED_EVIDENCE_INVALID,
            "isolated signer evidence lacks source-bound trust evidence",
        )
    if not _window(evidence.observed_at, evaluated_at, evidence.valid_until):
        return _Reason(
            CloseReasonCode.PRODUCER_ISOLATED_EVIDENCE_INVALID,
            "isolated signer evidence is not valid at evaluation time",
        )
    return None


def _authority_reason(
    authority: LifecycleAuthority, subject: DeploymentKey, evaluated_at: int
) -> AuthorityValidation | None:
    if authority.subject != subject:
        return _authority_deny("authority is bound to another deployment")
    if isinstance(authority, CreatorRollbackAuthority):
        if not _nonempty(
            authority.operation_id,
            authority.repository,
            authority.workload_class,
            authority.backend,
            authority.capability_reference,
        ):
            return _authority_deny("creator capability lacks operation or backend provenance")
        if (
            authority.binding_status is not CreationBindingStatus.CONFIRMED
            or authority.binding_mode
            not in {
                CreationBindingMode.TRANSACTION_EVENT,
                CreationBindingMode.EXACT_CHAIN_READBACK,
            }
            or not _nonempty(authority.binding_source, authority.binding_evidence_digest)
        ):
            return _authority_deny(
                "creator capability lacks positive same-operation creation binding"
            )
        if not _window(authority.prepared_at, evaluated_at, authority.valid_until):
            return _authority_deny("creator capability is not valid at evaluation time")
        trigger = authority.rollback_trigger
        if not isinstance(trigger, RollbackTriggerEvidence):
            return _authority_deny("creator rollback trigger is untyped")
        if trigger.handoff_state is not HandoffState.FAILED:
            return _authority_deny("creator rollback requires a failed handoff")
        if not _nonempty(trigger.source, trigger.evidence_digest):
            return _authority_deny("creator rollback trigger lacks source-bound evidence")
        if trigger.observed_at < authority.prepared_at or not _window(
            trigger.observed_at, evaluated_at, trigger.valid_until
        ):
            return _authority_deny("creator rollback trigger is not valid at evaluation time")
        return None
    if isinstance(authority, ProductionRetirementAuthority):
        if not _nonempty(
            authority.repository,
            authority.release,
            authority.environment,
            authority.authorization_id,
            authority.authorization_reference,
            authority.policy_reference,
            authority.approval_verification_source,
            authority.approval_verification_digest,
            authority.verified_authorization_id,
            authority.verified_environment,
            authority.verified_release,
            authority.single_use_authorization_id,
            authority.unique_authorization_id,
        ):
            return _authority_deny("production authority lacks identity or verification evidence")
        principals = (authority.requester, authority.approver, authority.executor)
        if any(not isinstance(item, ProductionPrincipal) for item in principals):
            return _authority_deny("production authority has untyped principals")
        if any(
            not _nonempty(item.principal, item.evidence_source, item.evidence_digest)
            or item.authentication_status is not VerificationStatus.VERIFIED
            for item in principals
        ):
            return _authority_hold("production principal authentication is missing or unverified")
        if authority.approver.kind is not PrincipalKind.HUMAN:
            return _authority_deny("production approver is not an authenticated human")
        if authority.approver.approval_role_membership is MembershipStatus.UNKNOWN:
            return _authority_hold("production approver role is unverified")
        if authority.approver.approval_role_membership is not MembershipStatus.VERIFIED_MEMBER:
            return _authority_deny("production human lacks the approval role")
        if authority.executor.kind is not PrincipalKind.MACHINE:
            return _authority_deny("production executor must be a machine principal")
        if authority.executor.principal == authority.approver.principal:
            return _authority_deny("production executor is the approving principal")
        if (
            authority.executor.admin_membership is MembershipStatus.VERIFIED_MEMBER
            or authority.executor.approval_role_membership is MembershipStatus.VERIFIED_MEMBER
        ):
            return _authority_deny("production executor is an admin or approver")
        if (
            authority.executor.admin_membership is not MembershipStatus.VERIFIED_NON_MEMBER
            or authority.executor.approval_role_membership
            is not MembershipStatus.VERIFIED_NON_MEMBER
        ):
            return _authority_hold("production executor non-admin role evidence is unverified")
        if (
            authority.requester.kind is PrincipalKind.MACHINE
            and authority.requester.principal == authority.approver.principal
        ):
            return _authority_deny("automated production requester cannot be its approver")
        if authority.action is not ProductionAuthorizationAction.PRODUCTION_RETIREMENT:
            return _authority_deny("production authority action disagrees")
        if (
            authority.verified_authorization_id != authority.authorization_id
            or authority.verified_environment != authority.environment
            or authority.verified_release != authority.release
            or authority.verified_subject != authority.subject
            or authority.verified_action != authority.action
        ):
            return _authority_deny("production approval verification is bound to another action")
        if authority.approval_verification_status is not VerificationStatus.VERIFIED:
            if authority.approval_verification_status is VerificationStatus.UNKNOWN:
                return _authority_hold("production approval is unverified")
            return _authority_deny("production approval verification failed")
        if authority.single_use_verification is not VerificationStatus.VERIFIED:
            if authority.single_use_verification is VerificationStatus.UNKNOWN:
                return _authority_hold("production single-use policy is unverified")
            return _authority_deny("production single-use verification failed")
        if authority.single_use_authorization_id != authority.authorization_id:
            return _authority_deny("production single-use proof is bound to another authorization")
        if authority.authorization_uniqueness_status is UniquenessStatus.UNKNOWN:
            return _authority_hold("production authorization uniqueness is unverified")
        if authority.authorization_uniqueness_status is UniquenessStatus.REUSED:
            return _authority_hold(
                "production authorization was replayed",
                CloseReasonCode.AUTHORITY_REPLAYED,
            )
        if authority.authorization_uniqueness_status is not UniquenessStatus.UNIQUE:
            return _authority_deny("production authorization uniqueness is invalid")
        if authority.unique_authorization_id != authority.authorization_id:
            return _authority_deny("production uniqueness proof is bound to another authorization")
        if type(authority.issued_at) is not int or authority.issued_at <= 0:
            return _authority_deny("production authorization issued-at is invalid")
        if authority.issued_at > authority.observed_at or not _window(
            authority.observed_at, evaluated_at, authority.valid_until
        ):
            return _authority_deny("production authorization is not valid at evaluation time")
        if authority.approval_mode is ProductionApprovalMode.GITHUB_ENVIRONMENT:
            if not isinstance(authority.bypass, ProductionBypassEvidence):
                return _authority_hold("GitHub environment bypass evidence is missing or untyped")
            if authority.prevent_self_review_status not in {
                EnvironmentControlStatus.VERIFIED_ENABLED,
                EnvironmentControlStatus.VERIFIED_DISABLED,
            }:
                return _authority_hold("GitHub environment self-review policy is unverified")
            if (
                authority.prevent_self_review_status is EnvironmentControlStatus.VERIFIED_DISABLED
                and authority.requester.kind is not PrincipalKind.HUMAN
                and authority.requester.principal == authority.approver.principal
            ):
                return _authority_deny(
                    "disabled self-review prevention permits automated self-approval"
                )
            bypass = authority.bypass
            if not isinstance(bypass, ProductionBypassEvidence) or not _nonempty(
                bypass.observation_source, bypass.observation_digest
            ):
                return _authority_hold("GitHub environment bypass evidence is missing")
            if bypass.observation_verification_status is not VerificationStatus.VERIFIED:
                return _authority_hold("GitHub environment bypass observation is unverified")
            if bypass.configuration_status is EnvironmentControlStatus.UNKNOWN:
                return _authority_hold("GitHub environment admin bypass policy is unverified")
            if bypass.configuration_status is EnvironmentControlStatus.VERIFIED_DISABLED:
                if bypass.use_status is not BypassUseStatus.UNUSED:
                    return _authority_deny(
                        "disabled GitHub admin bypass has inconsistent use evidence"
                    )
            elif bypass.configuration_status is EnvironmentControlStatus.VERIFIED_ENABLED:
                if bypass.enabled_bypass_policy_status is not VerificationStatus.VERIFIED:
                    return _authority_hold(
                        "enabled GitHub admin bypass lacks explicit policy authorization"
                    )
                if (
                    authority.requester.kind is PrincipalKind.MACHINE
                    and authority.requester.admin_membership is MembershipStatus.VERIFIED_MEMBER
                ):
                    return _authority_deny(
                        "automated requester is an admin while bypass is enabled"
                    )
                if (
                    authority.requester.kind is PrincipalKind.MACHINE
                    and authority.requester.admin_membership
                    is not MembershipStatus.VERIFIED_NON_MEMBER
                ):
                    return _authority_hold("automated requester admin status is unverified")
                if bypass.use_status is BypassUseStatus.USED:
                    if not _nonempty(bypass.actor_principal):
                        return _authority_hold("GitHub admin bypass actor is unknown")
                    if bypass.actor_principal != authority.approver.principal:
                        return _authority_deny(
                            "GitHub admin bypass actor is not the authorized human"
                        )
                elif bypass.use_status is BypassUseStatus.UNUSED:
                    if bypass.actor_principal is not None:
                        return _authority_deny("unused GitHub admin bypass names an actor")
                else:
                    return _authority_hold("GitHub admin bypass use is unknown")
            else:
                return _authority_deny("GitHub environment admin bypass policy is unsupported")
            if (
                not _nonempty(authority.workflow_ref, authority.ref)
                or not isinstance(authority.workflow_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", authority.workflow_sha) is None
            ):
                return _authority_deny("GitHub production approval lacks workflow and ref binding")
            if (
                authority.verified_workflow_ref,
                authority.verified_workflow_sha,
                authority.verified_ref,
            ) != (authority.workflow_ref, authority.workflow_sha, authority.ref):
                return _authority_deny(
                    "GitHub approval verification is bound to another workflow or ref"
                )
        elif authority.approval_mode is ProductionApprovalMode.EXTERNAL:
            if (
                authority.prevent_self_review_status is not EnvironmentControlStatus.NOT_APPLICABLE
                or authority.bypass is not None
            ):
                return _authority_deny(
                    "external production approval carries GitHub environment controls"
                )
            if any(
                value is not None
                for value in (
                    authority.workflow_ref,
                    authority.workflow_sha,
                    authority.ref,
                    authority.verified_workflow_ref,
                    authority.verified_workflow_sha,
                    authority.verified_ref,
                )
            ):
                return _authority_deny("external production approval carries GitHub-only bindings")
        else:
            return _authority_deny("production approval mode is unsupported")
        return None
    if not _nonempty(authority.source, authority.observation_digest):
        return _authority_hold("lifecycle authority lacks source-bound observation evidence")
    if not _window(authority.observed_at, evaluated_at, authority.valid_until):
        return _authority_hold("lifecycle authority is not valid at evaluation time")
    return None


def _class_denial(intent: CloseIntent, workload_class: str) -> _Reason | None:
    permitted = {
        CloseIntent.CI_CLEANUP: {"ci-runner", "ci-payload"},
        CloseIntent.STAGING_RETIREMENT: {"staging-payload"},
        CloseIntent.PRODUCTION_RETIREMENT: {"prod-payload"},
    }
    if intent in permitted and workload_class not in permitted[intent]:
        return _Reason(
            CloseReasonCode.CLASS_POLICY_FORBIDS_INTENT,
            f"{intent.value} cannot retire {workload_class}",
        )
    return None


def _authorize(
    *,
    subject: DeploymentKey,
    population: Population,
    candidates: CandidatePopulation,
    identity: Identity,
    intent: CloseIntent,
    authority: LifecycleAuthority | None,
    attestation: CreationAttestation | None,
    producer_environment: str | None,
    chain: ChainEvidence | None,
    evaluated_at: int,
) -> CloseDecision:
    def result(
        disposition: CloseDisposition,
        reason_code: CloseReasonCode,
        message: str,
    ) -> CloseDecision:
        return _decision(
            disposition,
            intent,
            subject,
            population,
            candidates,
            reason_code,
            message,
            evaluated_at=evaluated_at,
            identity=identity,
            authority=authority,
            chain=chain,
        )

    if authority is None:
        return result(
            CloseDisposition.HOLD,
            CloseReasonCode.AUTHORITY_MISSING,
            "lifecycle authority is missing",
        )
    if not isinstance(
        authority,
        (
            CIAuthority,
            StagingRetirementAuthority,
            ProductionRetirementAuthority,
            CreatorRollbackAuthority,
        ),
    ):
        raise ValueError("lifecycle authority must use a typed authority variant")
    validation = _authority_reason(authority, subject, evaluated_at)
    if validation:
        return result(
            CloseDisposition.HOLD if validation.evidence_gap else CloseDisposition.DENY,
            validation.reason_code,
            validation.message,
        )
    if intent is CloseIntent.CI_CLEANUP:
        if not isinstance(authority, CIAuthority):
            return result(
                CloseDisposition.DENY, CloseReasonCode.AUTHORITY_WRONG_TYPE, "wrong authority type"
            )
        if (authority.repository, authority.run, authority.attempt) != (
            identity.owner,
            identity.run,
            identity.attempt,
        ):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "CI lifecycle authority disagrees",
            )
        if type(authority.run) is not int or type(authority.attempt) is not int:
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_INVALID,
                "CI lifecycle numbers are not canonical integers",
            )
        if authority.run_state is not RunState.TERMINAL:
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_INVALID,
                "owning CI run is live or unknown",
            )
        if authority.consumer_state is not ConsumerState.FINISHED:
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_INVALID,
                "CI consumers are active or unknown",
            )
    elif intent is CloseIntent.STAGING_RETIREMENT:
        if not isinstance(authority, StagingRetirementAuthority) or (
            authority.repository,
            authority.release,
        ) != (identity.owner, identity.release):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "staging lifecycle authority disagrees",
            )
        if not _nonempty(authority.authorization_reference):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_INVALID,
                "staging authority has no reference",
            )
        if (
            attestation
            and attestation.authorization_reference != candidates.creation_authorization_reference
        ):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "staging authorities disagree",
            )
    elif intent is CloseIntent.PRODUCTION_RETIREMENT:
        if not isinstance(authority, ProductionRetirementAuthority) or (
            authority.repository,
            authority.release,
        ) != (identity.owner, identity.release):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "production lifecycle authority disagrees",
            )
        if producer_environment != authority.environment:
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "production environment authority disagrees",
            )
        if not _nonempty(authority.authorization_reference):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_INVALID,
                "production authority has no reference",
            )
    elif intent is CloseIntent.CREATOR_ROLLBACK:
        if not isinstance(authority, CreatorRollbackAuthority):
            return result(
                CloseDisposition.DENY, CloseReasonCode.AUTHORITY_WRONG_TYPE, "wrong authority type"
            )
        if authority.signer_owner != subject.owner:
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "creator signer owner disagrees",
            )
        if authority.group_identity_digest != canonical_group_identity_digest(population):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "creator group population disagrees",
            )
        if authority.operation_id != candidates.operation_id:
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "creator operation authority disagrees",
            )
        if (
            authority.repository,
            authority.workload_class,
            authority.run,
            authority.run_attempt,
            authority.release,
            authority.expires,
        ) != (
            identity.owner,
            identity.workload_class,
            identity.run,
            identity.attempt,
            identity.release,
            identity.expires,
        ):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                "creator lifecycle authority disagrees",
            )
        expected_creation_authority = candidates.creation_authorization_reference
        if identity.workload_class in {"staging-payload", "prod-payload"}:
            if not _nonempty(
                authority.creation_authorization_reference,
                expected_creation_authority,
            ):
                return result(
                    CloseDisposition.DENY,
                    CloseReasonCode.AUTHORITY_INVALID,
                    "payload creator rollback lacks original creation authorization",
                )
            if authority.creation_authorization_reference != expected_creation_authority:
                return result(
                    CloseDisposition.DENY,
                    CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                    "creator rollback creation authorization disagrees",
                )
            if attestation and (
                attestation.authorization_reference != expected_creation_authority
            ):
                return result(
                    CloseDisposition.DENY,
                    CloseReasonCode.AUTHORITY_IDENTITY_DISAGREEMENT,
                    "creator rollback attestation authorization disagrees",
                )
        elif (
            authority.creation_authorization_reference is not None
            or expected_creation_authority is not None
        ):
            return result(
                CloseDisposition.DENY,
                CloseReasonCode.AUTHORITY_INVALID,
                "CI creator rollback carries payload authorization",
            )
    else:  # pragma: no cover
        raise AssertionError(f"unhandled close intent {intent!r}")
    return result(CloseDisposition.ALLOW, CloseReasonCode.AUTHORIZATION_SUCCEEDED, "authorized")


def evaluate_close(
    *,
    subject: DeploymentKey,
    population: Population,
    candidates: CandidatePopulation,
    intent: CloseIntent,
    authority: LifecycleAuthority | None,
    chain_evidence: ChainEvidence | None,
    producer_authentication: ProducerAuthentication | None,
    evaluated_at: int,
) -> CloseDecision:
    """Return a typed decision without performing destructive I/O."""

    if not isinstance(subject, DeploymentKey) or not isinstance(intent, CloseIntent):
        raise ValueError("exact subject and explicit CloseIntent are required")
    if type(evaluated_at) is not int or evaluated_at <= 0:
        raise ValueError("evaluated_at must be canonical positive Unix seconds")
    if (
        not isinstance(candidates, CandidatePopulation)
        or not isinstance(candidates.completeness, CandidateCompleteness)
        or not isinstance(candidates.uniqueness_status, UniquenessStatus)
    ):
        raise ValueError("typed attribution candidate evidence is required")
    if type(candidates.count) is not int or candidates.count < 0:
        raise ValueError("candidate count must be a canonical nonnegative integer")
    identity, reason = _classified(population)
    if identity is None:
        if reason is None:  # pragma: no cover - closed internal result type
            raise AssertionError("classification failure requires a reason")
        return _decision(
            CloseDisposition.HOLD,
            intent,
            subject,
            population,
            candidates,
            reason.reason_code,
            reason.message,
            evaluated_at=evaluated_at,
        )
    denial = _class_denial(intent, identity.workload_class)
    if denial:
        return _decision(
            CloseDisposition.DENY,
            intent,
            subject,
            population,
            candidates,
            denial.reason_code,
            denial.message,
            evaluated_at=evaluated_at,
            identity=identity,
        )
    group_digest = canonical_group_identity_digest(population)
    if (
        candidates.completeness is not CandidateCompleteness.COMPLETE
        or candidates.count != 1
        or candidates.uniqueness_status is not UniquenessStatus.UNIQUE
        or not _nonempty(
            candidates.operation_id,
            candidates.prepared_operation_key,
            candidates.evidence_digest,
        )
        or type(candidates.operation_ordinal) is not int
        or candidates.operation_ordinal <= 0
        or candidates.subject != subject
        or candidates.group_identity_digest != group_digest
        or (
            identity.workload_class in {"staging-payload", "prod-payload"}
            and not _nonempty(candidates.creation_authorization_reference)
        )
        or (
            identity.workload_class.startswith("ci-")
            and candidates.creation_authorization_reference is not None
        )
        or candidates.prepared_operation_key
        != canonical_prepared_operation_key(
            signer_owner=subject.owner,
            producer_repository=identity.owner,
            workload_class=identity.workload_class,
            operation_id=candidates.operation_id,
            operation_ordinal=candidates.operation_ordinal,
            prepared_group_identity_digest=group_digest,
            run=identity.run,
            run_attempt=identity.attempt,
            release=identity.release,
        )
    ):
        return _decision(
            CloseDisposition.HOLD,
            intent,
            subject,
            population,
            candidates,
            CloseReasonCode.CANDIDATE_INCOMPLETE_OR_NON_UNIQUE,
            "deployment attribution population is incomplete or not unique",
            evaluated_at=evaluated_at,
            identity=identity,
        )
    if intent is not CloseIntent.CREATOR_ROLLBACK and chain_evidence is None:
        return _decision(
            CloseDisposition.HOLD,
            intent,
            subject,
            population,
            candidates,
            CloseReasonCode.CHAIN_EVIDENCE_MISSING,
            "independent pre-close chain evidence is missing",
            evaluated_at=evaluated_at,
            identity=identity,
        )
    if chain_evidence is not None:
        if (
            not isinstance(chain_evidence, ChainEvidence)
            or not isinstance(chain_evidence.agreement, SourceAgreement)
            or not isinstance(chain_evidence.proof_mode, ChainProofMode)
            or not isinstance(chain_evidence.deployment_state, PreCloseDeploymentState)
            or not isinstance(chain_evidence.lease_completeness, LeasePopulationCompleteness)
        ):
            raise ValueError("typed chain evidence with explicit states is required")
        base = dict(
            evaluated_at=evaluated_at,
            identity=identity,
            chain=chain_evidence,
        )
        if (
            chain_evidence.subject != subject
            or chain_evidence.group_identity_digest != group_digest
        ):
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.CHAIN_EVIDENCE_WRONG_POPULATION,
                "pre-close evidence is bound to another population",
                **base,
            )
        if (
            not _nonempty(
                chain_evidence.chain_id,
                chain_evidence.source_a,
                chain_evidence.source_b,
                chain_evidence.trust_path_a,
                chain_evidence.trust_path_b,
            )
            or chain_evidence.source_a == chain_evidence.source_b
            or chain_evidence.trust_path_a == chain_evidence.trust_path_b
            or chain_evidence.proof_mode is not ChainProofMode.EXACT_FINALIZED_HEIGHT
            or type(chain_evidence.source_a_height) is not int
            or type(chain_evidence.source_b_height) is not int
            or type(chain_evidence.common_finality_height) is not int
            or chain_evidence.common_finality_height <= 0
            or not (
                chain_evidence.source_a_height
                == chain_evidence.source_b_height
                == chain_evidence.common_finality_height
            )
        ):
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.CHAIN_EVIDENCE_TRUST_PATHS_INCOMPLETE,
                "pre-close evidence lacks two independent finalized trust paths",
                **base,
            )
        if not _nonempty(
            chain_evidence.evidence_digest,
            chain_evidence.deployment_population_digest,
            chain_evidence.deployment_state_digest,
            chain_evidence.group_population_digest,
            chain_evidence.group_state_digest,
            chain_evidence.lease_population_digest,
            chain_evidence.lease_state_digest,
        ) or not _window(chain_evidence.observed_at, evaluated_at, chain_evidence.valid_until):
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.CHAIN_EVIDENCE_DIGEST_OR_INTERVAL_INCOMPLETE,
                "pre-close evidence lacks digest or interval",
                **base,
            )
        if (
            type(chain_evidence.deployment_count) is not int
            or chain_evidence.deployment_count != 1
            or chain_evidence.deployment_population_digest
            != canonical_deployment_population_digest(subject)
            or chain_evidence.deployment_state is not PreCloseDeploymentState.ACTIVE
            or type(chain_evidence.group_count) is not int
            or chain_evidence.group_count <= 0
            or chain_evidence.group_count != population.observed_count
            or chain_evidence.group_population_digest != group_digest
        ):
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.CHAIN_POPULATION_INCOMPLETE_OR_MISMATCHED,
                "deployment or group population is incomplete or mismatched",
                **base,
            )
        leases_complete = (
            chain_evidence.lease_completeness is LeasePopulationCompleteness.COMPLETE
            and type(chain_evidence.lease_count) is int
            and chain_evidence.lease_count > 0
            and chain_evidence.lease_population_digest != EMPTY_POPULATION_DIGEST
        )
        never_leased = (
            chain_evidence.lease_completeness is LeasePopulationCompleteness.EXACT_NEVER_LEASED
            and type(chain_evidence.lease_count) is int
            and chain_evidence.lease_count == 0
            and chain_evidence.lease_population_digest == EMPTY_POPULATION_DIGEST
        )
        if not leases_complete and not never_leased:
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.CHAIN_LEASE_POPULATION_INCOMPLETE,
                "lease population is incomplete, empty, or vacuous",
                **base,
            )
        if chain_evidence.agreement is not SourceAgreement.AGREEING:
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.CHAIN_SOURCES_DISAGREE,
                "two complete chain sources do not agree",
                **base,
            )
    attestation = None
    producer_environment = None
    if intent is not CloseIntent.CREATOR_ROLLBACK:
        if producer_authentication is None:
            return _decision(
                CloseDisposition.HOLD,
                intent,
                subject,
                population,
                candidates,
                CloseReasonCode.PRODUCER_UNAUTHENTICATED,
                "producer is unauthenticated",
                evaluated_at=evaluated_at,
                identity=identity,
                authority=authority,
                chain=chain_evidence,
            )
        if isinstance(producer_authentication, SharedSignerEvidence):
            reason = _shared_reason(
                producer_authentication,
                subject,
                population,
                identity,
                candidates,
                evaluated_at,
            )
            if reason:
                return _decision(
                    CloseDisposition.HOLD,
                    intent,
                    subject,
                    population,
                    candidates,
                    reason.reason_code,
                    reason.message,
                    evaluated_at=evaluated_at,
                    identity=identity,
                    authority=authority,
                    chain=chain_evidence,
                )
            attestation = producer_authentication.attestation
            producer_environment = attestation.environment
        elif isinstance(producer_authentication, IsolatedSignerEvidence):
            reason = _isolated_reason(
                producer_authentication, subject, identity, candidates, evaluated_at
            )
            if reason:
                return _decision(
                    CloseDisposition.HOLD,
                    intent,
                    subject,
                    population,
                    candidates,
                    reason.reason_code,
                    reason.message,
                    evaluated_at=evaluated_at,
                    identity=identity,
                    authority=authority,
                    chain=chain_evidence,
                )
            producer_environment = producer_authentication.environment
        else:
            raise ValueError("producer authentication must use typed evidence")
    elif producer_authentication is not None:
        if isinstance(producer_authentication, SharedSignerEvidence):
            reason = _shared_reason(
                producer_authentication,
                subject,
                population,
                identity,
                candidates,
                evaluated_at,
            )
            if reason:
                return _decision(
                    CloseDisposition.HOLD,
                    intent,
                    subject,
                    population,
                    candidates,
                    reason.reason_code,
                    reason.message,
                    evaluated_at=evaluated_at,
                    identity=identity,
                    authority=authority,
                    chain=chain_evidence,
                )
            attestation = producer_authentication.attestation
        elif not isinstance(producer_authentication, IsolatedSignerEvidence):
            raise ValueError("producer authentication must use typed evidence")
    return _authorize(
        subject=subject,
        population=population,
        candidates=candidates,
        identity=identity,
        intent=intent,
        authority=authority,
        attestation=attestation,
        producer_environment=producer_environment,
        chain=chain_evidence,
        evaluated_at=evaluated_at,
    )


__all__ = [
    "CLOSE_POLICY_VERSION",
    "AttestationVerification",
    "BypassUseStatus",
    "EnvironmentControlStatus",
    "CIAuthority",
    "CandidateCompleteness",
    "CandidatePopulation",
    "ChainEvidence",
    "ChainProofMode",
    "CloseDecision",
    "CloseDisposition",
    "CloseIntent",
    "CloseReasonCode",
    "ConsumerState",
    "CreationAttestation",
    "CreationBindingMode",
    "CreationBindingStatus",
    "CreatorRollbackAuthority",
    "HandoffState",
    "EMPTY_POPULATION_DIGEST",
    "IsolatedSignerEvidence",
    "LeasePopulationCompleteness",
    "LifecycleAuthority",
    "MembershipStatus",
    "ProducerAuthentication",
    "ProductionRetirementAuthority",
    "PreCloseDeploymentState",
    "ProductionApprovalMode",
    "ProductionAuthorizationAction",
    "ProductionBypassEvidence",
    "ProductionPrincipal",
    "RunState",
    "PrincipalKind",
    "RollbackTriggerEvidence",
    "SharedSignerEvidence",
    "SourceAgreement",
    "StagingRetirementAuthority",
    "UniquenessStatus",
    "VerificationStatus",
    "canonical_attestation_digest",
    "canonical_prepared_operation_key",
    "canonical_deployment_population_digest",
    "canonical_group_identity_digest",
    "evaluate_close",
]
