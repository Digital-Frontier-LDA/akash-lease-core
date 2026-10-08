"""Pure, additive lifetime-liability data and retained financial transitions.

NONE of these records, decoded bytes, digests or returned proposals authenticate
an issuer, a financial basis, a store or an ACK. This module issues no signing or
broadcast capability. Adapters must authenticate their inputs, atomically commit
the financial pointer with the same owner/leaf admission head, independently
verify the ACK, and enforce one-use authority at the actual signing boundary.

A maximum proposal is acquired BEFORE owner reservation. Its full allocation
is retained once on the original creator leaf. Funding consumes that allocation;
it does not allocate another active/unresolved slot. Unknown attempts remain
charged and every attempt identity is retained, including proven non-effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .chain_identity import DeploymentKey, is_canonical_akash_owner
from .create_journal import BackendIdentity, PreparedCreate, canonical_journal_digest
from .creation_admission import AdmissionScope, PermitRevocationStatus
from .workload_identity import MAX_UTC_UNIX_SECONDS

FINANCIAL_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class FinancialTransitionError(ValueError):
    """A proposal would exceed or rewrite a retained financial binding."""


def _version(value: object) -> None:
    if type(value) is not int or value != FINANCIAL_SCHEMA_VERSION:
        raise ValueError("unsupported financial schema version")


def _text(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or len(value) > 256
        or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError(f"{name} must be bounded canonical ASCII")


def _digest(value: object) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("financial SHA-256 digest is invalid")


def _revision(value: object) -> None:
    if type(value) is not str or _REVISION.fullmatch(value) is None:
        raise ValueError("financial source revision is invalid")


def _uint(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("financial quantity must be a nonnegative integer")


def _time(value: object) -> None:
    if type(value) is not int or not 0 < value <= MAX_UTC_UNIX_SECONDS:
        raise ValueError("financial time must be canonical UTC seconds")


def _window(observed: int, until: int) -> None:
    _time(observed)
    _time(until)
    if until < observed:
        raise ValueError("financial evidence window is invalid")


def _owner(value: object) -> None:
    if not is_canonical_akash_owner(value):
        raise ValueError("financial owner must be canonical")


@dataclass(frozen=True, order=True)
class NativeLiability:
    """One native quantity; integer units, without conversion or policy defaults."""

    denomination: str
    amount: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if (
            type(self.denomination) is not str
            or not self.denomination
            or self.denomination != self.denomination.strip()
            or len(self.denomination.encode("utf-8")) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in self.denomination)
        ):
            raise ValueError("native denomination must have bounded canonical encoding")
        _uint(self.amount)


@dataclass(frozen=True)
class NativeLiabilityVector:
    quantities: tuple[NativeLiability, ...]
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.quantities) is not tuple or any(
            type(item) is not NativeLiability for item in self.quantities
        ):
            raise ValueError("native vector must contain exact typed quantities")
        names = tuple(item.denomination for item in self.quantities)
        if names != tuple(sorted(set(names))):
            raise ValueError("native denominations must be sorted and unique")

    def amount(self, denomination: str) -> int:
        return next((q.amount for q in self.quantities if q.denomination == denomination), 0)

    def covers(self, other: NativeLiabilityVector) -> bool:
        if type(other) is not NativeLiabilityVector:
            raise ValueError("native vector must be typed")
        names = {q.denomination for q in self.quantities}
        return all(
            q.denomination in names and q.amount <= self.amount(q.denomination)
            for q in other.quantities
        )


def add_native_liabilities(vectors: tuple[NativeLiabilityVector, ...]) -> NativeLiabilityVector:
    """Add already-attributed quantities, never infer economic attribution."""

    if type(vectors) is not tuple or any(type(v) is not NativeLiabilityVector for v in vectors):
        raise ValueError("typed native vector population is required")
    amounts: dict[str, int] = {}
    for vector in vectors:
        for quantity in vector.quantities:
            amounts[quantity.denomination] = (
                amounts.get(quantity.denomination, 0) + quantity.amount
            )
    return NativeLiabilityVector(tuple(NativeLiability(k, amounts[k]) for k in sorted(amounts)))


@dataclass(frozen=True)
class QualifiedUactBasis:
    """Claimed authenticated economic basis; construction does not qualify it.

    In particular, a mint followed by its deposit is not automatically two
    additive economic liabilities. The registered adapter must verify the exact
    attribution/conversion policy and source before presenting this record.
    """

    native_maximum: NativeLiabilityVector
    amount_uact: int
    policy_revision: str
    attribution_digest: str
    evidence_digest: str
    source: str
    observed_at: int
    valid_until: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.native_maximum) is not NativeLiabilityVector:
            raise ValueError("qualified basis native maximum must be typed")
        _uint(self.amount_uact)
        if self.amount_uact == 0:
            raise ValueError("qualified maximum must be positive")
        _revision(self.policy_revision)
        _digest(self.attribution_digest)
        _digest(self.evidence_digest)
        _text(self.source, "basis source")
        _window(self.observed_at, self.valid_until)


@dataclass(frozen=True)
class ServicePrincipalBinding:
    """A genuine service identity, separate from GitHub creator provenance."""

    issuer: str
    subject: str
    audience: str
    credential_fingerprint: str
    deployed_source_revision: str
    registration_digest: str
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for value in (self.issuer, self.subject, self.audience):
            _text(value, "service principal identity")
        _digest(self.credential_fingerprint)
        _revision(self.deployed_source_revision)
        _digest(self.registration_digest)


class FinancialEffectPurpose(str, Enum):
    CREATE = "create"
    DEPOSIT = "deposit"
    FEE_REFILL = "fee_refill"
    TRANSACTION_FEE = "transaction_fee"
    GRANT = "grant"
    MINT = "mint"
    AMENDMENT = "amendment"


@dataclass(frozen=True)
class FundingActorRegistration:
    actor_id: str
    chain_id: str
    owner: str
    backend: BackendIdentity
    principal: ServicePrincipalBinding
    cas_domain: str
    policy_revision: str
    writer_population_digest: str
    creator_leaf_digests: tuple[str, ...]
    signing_wallets: tuple[str, ...]
    purposes: tuple[FinancialEffectPurpose, ...]
    evidence_digest: str
    observed_at: int
    valid_until: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for value in (self.actor_id, self.chain_id, self.cas_domain):
            _text(value, "funding actor binding")
        _owner(self.owner)
        if (
            type(self.backend) is not BackendIdentity
            or type(self.principal) is not ServicePrincipalBinding
        ):
            raise ValueError("funding backend and principal must be exact typed data")
        _revision(self.policy_revision)
        _digest(self.writer_population_digest)
        _digest(self.evidence_digest)
        _window(self.observed_at, self.valid_until)
        for population in (self.creator_leaf_digests, self.signing_wallets):
            if (
                type(population) is not tuple
                or not population
                or population != tuple(sorted(set(population)))
            ):
                raise ValueError("funding registration population must be nonempty sorted unique")
        for digest in self.creator_leaf_digests:
            _digest(digest)
        for wallet in self.signing_wallets:
            _owner(wallet)
        if (
            type(self.purposes) is not tuple
            or not self.purposes
            or any(type(p) is not FinancialEffectPurpose for p in self.purposes)
            or self.purposes != tuple(sorted(set(self.purposes), key=lambda p: p.value))
        ):
            raise ValueError("funding purposes must be typed sorted unique")


@dataclass(frozen=True)
class ServicePrincipalObservation:
    principal: ServicePrincipalBinding
    revocation_epoch: int
    status: PermitRevocationStatus
    authentication_evidence_digest: str
    observed_at: int
    valid_until: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if (
            type(self.principal) is not ServicePrincipalBinding
            or type(self.status) is not PermitRevocationStatus
        ):
            raise ValueError("principal observation must be exact typed data")
        _uint(self.revocation_epoch)
        _digest(self.authentication_evidence_digest)
        _window(self.observed_at, self.valid_until)


@dataclass(frozen=True)
class OperationMaximumLiability:
    """Finite maximum PROPOSAL obtained before reservation, never spend authority."""

    quote_id: str
    prepared: PreparedCreate
    creator_leaf: AdmissionScope
    cas_domain: str
    policy_revision: str
    writer_population_digest: str
    native_maximum: NativeLiabilityVector
    uact_basis: QualifiedUactBasis | None
    evidence_digest: str
    observed_at: int
    valid_until: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _text(self.quote_id, "quote identity")
        _text(self.cas_domain, "CAS domain")
        _revision(self.policy_revision)
        _digest(self.writer_population_digest)
        _digest(self.evidence_digest)
        _window(self.observed_at, self.valid_until)
        if (
            type(self.prepared) is not PreparedCreate
            or type(self.creator_leaf) is not AdmissionScope
        ):
            raise ValueError("original prepared create and creator leaf must be typed")
        p, leaf = self.prepared, self.creator_leaf
        if (
            leaf.owner,
            leaf.producer_issuer,
            leaf.repository_owner_id,
            leaf.repository_id,
            leaf.repository,
            leaf.workload_class,
        ) != (
            p.owner_candidate.owner,
            p.producer.issuer,
            p.producer.repository_owner_id,
            p.producer.repository_id,
            p.producer.repository,
            p.lifecycle.workload_class,
        ):
            raise ValueError("maximum proposal differs from the original creator leaf")
        if type(self.native_maximum) is not NativeLiabilityVector or not any(
            q.amount > 0 for q in self.native_maximum.quantities
        ):
            raise ValueError("finite positive native maximum is required")
        if self.uact_basis is not None and (
            type(self.uact_basis) is not QualifiedUactBasis
            or self.uact_basis.native_maximum != self.native_maximum
            or self.uact_basis.policy_revision != self.policy_revision
        ):
            raise ValueError("qualified UACT basis differs from the exact native maximum/policy")


@dataclass(frozen=True)
class FinancialEffectIntent:
    operation_id: str
    effect_id: str
    attempt_id: str
    quote_digest: str
    actor_registration_digest: str
    principal: ServicePrincipalBinding
    revocation_epoch: int
    subject: DeploymentKey
    signing_wallet: str
    purpose: FinancialEffectPurpose
    message_digest: str
    sign_doc_digest: str
    fee_digest: str
    account_sequence: int
    maximum_debit: NativeLiabilityVector
    fee_debit: NativeLiabilityVector
    attribution_digest: str
    created_at: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for value in (self.operation_id, self.effect_id, self.attempt_id):
            _text(value, "financial attempt identity")
        for value in (
            self.quote_digest,
            self.actor_registration_digest,
            self.message_digest,
            self.sign_doc_digest,
            self.fee_digest,
            self.attribution_digest,
        ):
            _digest(value)
        if (
            type(self.principal) is not ServicePrincipalBinding
            or type(self.subject) is not DeploymentKey
        ):
            raise ValueError("effect principal and actual deployment must be typed")
        if type(self.purpose) is not FinancialEffectPurpose:
            raise ValueError("financial purpose must be typed")
        _owner(self.signing_wallet)
        _uint(self.revocation_epoch)
        _uint(self.account_sequence)
        _time(self.created_at)
        if (
            type(self.maximum_debit) is not NativeLiabilityVector
            or type(self.fee_debit) is not NativeLiabilityVector
        ):
            raise ValueError("financial debits must be typed vectors")
        if not self.maximum_debit.covers(self.fee_debit):
            raise ValueError("maximum debit must include its native fees")
        if (
            self.purpose is FinancialEffectPurpose.TRANSACTION_FEE
            and self.maximum_debit != self.fee_debit
        ):
            raise ValueError(
                "transaction fee intent must contain only its exact native fee vector"
            )


class FinancialEffectState(str, Enum):
    SIGN_RESERVED_UNKNOWN = "sign_reserved_unknown"
    SIGNED = "signed"
    BROADCAST_CLAIMED_UNKNOWN = "broadcast_claimed_unknown"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    PROVEN_NOT_SIGNED = "proven_not_signed"


@dataclass(frozen=True)
class FinancialEffectObservation:
    """Evidence data only. A registered adapter must verify it before CAS."""

    intent_digest: str
    state: FinancialEffectState
    evidence_digest: str
    observed_at: int
    transaction_hash: str | None
    actual_debit: NativeLiabilityVector | None
    transaction_code: int | None
    principal_observation: ServicePrincipalObservation | None
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _digest(self.intent_digest)
        _digest(self.evidence_digest)
        _time(self.observed_at)
        if (
            type(self.state) is not FinancialEffectState
            or self.state is FinancialEffectState.SIGN_RESERVED_UNKNOWN
        ):
            raise ValueError("observation state is invalid")
        if self.state is FinancialEffectState.PROVEN_NOT_SIGNED:
            if (
                self.transaction_hash is not None
                or self.transaction_code is not None
                or self.actual_debit is not None
            ):
                raise ValueError("non-sign proof cannot contain transaction outcome")
        else:
            _digest(self.transaction_hash)
        terminal = self.state in (FinancialEffectState.CONFIRMED, FinancialEffectState.FAILED)
        if terminal:
            if type(self.actual_debit) is not NativeLiabilityVector:
                raise ValueError("terminal outcome requires complete native debit")
            _uint(self.transaction_code)
            if self.transaction_code is None:
                raise ValueError("terminal transaction code is missing")
            if self.transaction_code > 2**32 - 1:
                raise ValueError("transaction code must be uint32")
            if (self.transaction_code == 0) != (self.state is FinancialEffectState.CONFIRMED):
                raise ValueError("transaction outcome and code disagree")
        elif self.actual_debit is not None or self.transaction_code is not None:
            raise ValueError("inflight outcome cannot claim settlement")
        if self.state is FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN:
            if type(self.principal_observation) is not ServicePrincipalObservation:
                raise ValueError("broadcast claim requires fresh principal observation data")
        elif self.principal_observation is not None:
            raise ValueError("principal observation belongs only to a broadcast claim")


@dataclass(frozen=True)
class FinancialEffectRecord:
    intent: FinancialEffectIntent
    observations: tuple[FinancialEffectObservation, ...]
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.intent) is not FinancialEffectIntent or type(self.observations) is not tuple:
            raise ValueError("retained financial effect must be typed")
        prior = FinancialEffectState.SIGN_RESERVED_UNKNOWN
        previous_time = self.intent.created_at
        transaction = None
        for observation in self.observations:
            if type(observation) is not FinancialEffectObservation:
                raise ValueError("retained observation must be typed")
            allowed = {
                FinancialEffectState.SIGN_RESERVED_UNKNOWN: (
                    FinancialEffectState.SIGNED,
                    FinancialEffectState.PROVEN_NOT_SIGNED,
                ),
                FinancialEffectState.SIGNED: (FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,),
                FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN: (
                    FinancialEffectState.CONFIRMED,
                    FinancialEffectState.FAILED,
                ),
            }.get(prior, ())
            if (
                observation.state not in allowed
                or observation.observed_at < previous_time
                or observation.intent_digest != canonical_journal_digest(self.intent)
            ):
                raise ValueError("financial effect history skips or rewrites a transition")
            if transaction is not None and observation.transaction_hash != transaction:
                raise ValueError("financial transaction hash cannot change")
            if observation.transaction_hash is not None:
                transaction = observation.transaction_hash
            principal = observation.principal_observation
            if principal is not None and (
                principal.principal != self.intent.principal
                or principal.revocation_epoch != self.intent.revocation_epoch
                or principal.status is not PermitRevocationStatus.ACTIVE
                or not principal.observed_at <= observation.observed_at <= principal.valid_until
            ):
                raise ValueError("broadcast principal is stale, revoked or differently bound")
            if observation.actual_debit is not None and (
                not self.intent.maximum_debit.covers(observation.actual_debit)
                or not observation.actual_debit.covers(self.intent.fee_debit)
            ):
                raise ValueError("actual debit must remain within the maximum and cover fees")
            prior = observation.state
            previous_time = observation.observed_at

    @property
    def state(self) -> FinancialEffectState:
        return (
            self.observations[-1].state
            if self.observations
            else FinancialEffectState.SIGN_RESERVED_UNKNOWN
        )

    @property
    def charged_debit(self) -> NativeLiabilityVector:
        if self.state is FinancialEffectState.PROVEN_NOT_SIGNED:
            return NativeLiabilityVector(())
        if self.state in (FinancialEffectState.CONFIRMED, FinancialEffectState.FAILED):
            debit = self.observations[-1].actual_debit
            if debit is None:  # Constructor enforces this; keep the return type exact.
                raise ValueError("terminal debit is missing")
            return debit
        return self.intent.maximum_debit


@dataclass(frozen=True)
class FinancialOperationState:
    maximum: OperationMaximumLiability
    revision: int
    previous_digest: str | None
    effects: tuple[FinancialEffectRecord, ...]
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if type(self.maximum) is not OperationMaximumLiability:
            raise ValueError("operation maximum must be exact typed data")
        _uint(self.revision)
        if self.revision == 0:
            if self.previous_digest is not None or self.effects:
                raise ValueError("initial financial proposal cannot contain history")
        else:
            _digest(self.previous_digest)
        if type(self.effects) is not tuple or any(
            type(e) is not FinancialEffectRecord for e in self.effects
        ):
            raise ValueError("complete retained effect tuple is required")
        if self.revision != sum(1 + len(e.observations) for e in self.effects):
            raise ValueError("financial revision disagrees with its complete retained events")
        ids = tuple(e.intent.effect_id for e in self.effects)
        attempts = tuple(e.intent.attempt_id for e in self.effects)
        if len(set(ids)) != len(ids) or len(set(attempts)) != len(attempts):
            raise ValueError("effect and attempt identities are permanent and unique")
        docs = tuple(e.intent.sign_doc_digest for e in self.effects)
        sequences = tuple(
            (e.intent.signing_wallet, e.intent.account_sequence) for e in self.effects
        )
        if len(set(docs)) != len(docs) or len(set(sequences)) != len(sequences):
            raise ValueError("retained signing document or wallet sequence cannot be reused")
        q = self.maximum
        subjects = {e.intent.subject for e in self.effects}
        if len(subjects) > 1:
            raise ValueError("operation cannot switch deployment identity")
        if self.effects and self.effects[0].intent.purpose is not FinancialEffectPurpose.CREATE:
            raise ValueError("retained mapping must begin with the actual original create")
        for effect in self.effects[1:]:
            if effect.intent.purpose in (
                FinancialEffectPurpose.CREATE,
                FinancialEffectPurpose.AMENDMENT,
            ):
                raise ValueError("replacement create or cap amendment is unsupported")
            initial = self.effects[0]
            if (
                initial.state is not FinancialEffectState.CONFIRMED
                or initial.observations[-1].observed_at > effect.intent.created_at
            ):
                raise ValueError("funding cannot predate confirmed original create mapping")
        for effect in self.effects:
            if (
                effect.intent.operation_id != q.prepared.operation_id
                or effect.intent.quote_digest != canonical_journal_digest(q)
                or effect.intent.subject.owner != q.creator_leaf.owner
            ):
                raise ValueError("effect differs from its original operation/maximum")
        if not q.native_maximum.covers(financial_native_usage(self)):
            raise ValueError("retained effects exceed the operation lifetime maximum")


def financial_native_usage(state: FinancialOperationState) -> NativeLiabilityVector:
    """Within-allocation usage, not another owner/leaf capacity reservation."""

    if type(state) is not FinancialOperationState:
        raise ValueError("financial state must be typed")
    return add_native_liabilities(tuple(effect.charged_debit for effect in state.effects))


@dataclass(frozen=True)
class FinancialCommitBinding:
    """Retained claimed ACK binding; never proof authentication or capability."""

    cas_domain: str
    chain_id: str
    owner: str
    operation_id: str
    owner_revision: int
    owner_state_digest: str
    financial_revision: int
    financial_state_digest: str
    receipt_digest: str
    observed_at: int
    schema_version: int = FINANCIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for value in (self.cas_domain, self.chain_id, self.operation_id):
            _text(value, "financial commit identity")
        _owner(self.owner)
        _uint(self.owner_revision)
        if self.owner_revision == 0:
            raise ValueError("financial allocation must bind a committed owner revision")
        _uint(self.financial_revision)
        for value in (self.owner_state_digest, self.financial_state_digest, self.receipt_digest):
            _digest(value)
        _time(self.observed_at)


def validate_financial_commit_binding(
    state: FinancialOperationState, commit: FinancialCommitBinding
) -> None:
    """Check DATA equality only. The broker separately verifies signed ACK/store."""

    if type(state) is not FinancialOperationState or type(commit) is not FinancialCommitBinding:
        raise ValueError("financial commit data must be typed")
    q = state.maximum
    if (
        commit.cas_domain,
        commit.chain_id,
        commit.owner,
        commit.operation_id,
        commit.financial_revision,
        commit.financial_state_digest,
    ) != (
        q.cas_domain,
        q.creator_leaf.chain_id,
        q.creator_leaf.owner,
        q.prepared.operation_id,
        state.revision,
        canonical_journal_digest(state),
    ):
        raise FinancialTransitionError("financial ACK data does not bind this exact state")
    latest = max(
        (o.observed_at for e in state.effects for o in e.observations),
        default=state.maximum.observed_at,
    )
    latest = max((latest, *(e.intent.created_at for e in state.effects)))
    if commit.observed_at < latest:
        raise FinancialTransitionError("financial ACK data predates its retained state")


def propose_operation_maximum(
    maximum: OperationMaximumLiability, *, now: int
) -> FinancialOperationState:
    """Propose reservation of one full maximum; does not reserve or activate it."""

    _time(now)
    if type(maximum) is not OperationMaximumLiability:
        raise ValueError("maximum proposal must be typed")
    if not maximum.observed_at <= now <= maximum.valid_until:
        raise FinancialTransitionError("maximum proposal is stale or future")
    if (
        maximum.uact_basis is not None
        and not maximum.uact_basis.observed_at <= now <= maximum.uact_basis.valid_until
    ):
        raise FinancialTransitionError("UACT basis is stale or future")
    return FinancialOperationState(maximum, 0, None, ())


def propose_financial_effect(
    state: FinancialOperationState,
    intent: FinancialEffectIntent,
    registration: FundingActorRegistration,
    observation: ServicePrincipalObservation,
    commit: FinancialCommitBinding,
    *,
    now: int,
) -> FinancialOperationState:
    """Propose UNKNOWN before signing; output is never signing permission.

    The maximum's acquisition window governs initial reservation. Later funding
    uses the same retained lifetime cap and requires current actor/policy evidence
    at the real signing boundary; rolling time never resets that allocation.
    """

    _time(now)
    validate_financial_commit_binding(state, commit)
    if commit.observed_at > now:
        raise FinancialTransitionError("financial ACK data is from the future")
    if (
        type(intent) is not FinancialEffectIntent
        or type(registration) is not FundingActorRegistration
        or type(observation) is not ServicePrincipalObservation
    ):
        raise ValueError("effect proposal inputs must be exact typed data")
    q = state.maximum
    if (
        intent.principal != registration.principal
        or observation.principal != registration.principal
        or intent.actor_registration_digest != canonical_journal_digest(registration)
        or intent.revocation_epoch != observation.revocation_epoch
        or observation.status is not PermitRevocationStatus.ACTIVE
        or not observation.observed_at <= now <= observation.valid_until
        or not registration.observed_at <= now <= registration.valid_until
        or intent.created_at != now
    ):
        raise FinancialTransitionError("funding principal is stale, revoked or differently bound")
    if (
        registration.chain_id,
        registration.owner,
        registration.backend,
        registration.cas_domain,
        registration.policy_revision,
        registration.writer_population_digest,
    ) != (
        q.creator_leaf.chain_id,
        q.creator_leaf.owner,
        q.prepared.owner_candidate.backend,
        q.cas_domain,
        q.policy_revision,
        q.writer_population_digest,
    ):
        raise FinancialTransitionError("funding actor differs from operation policy/source/domain")
    if (
        canonical_journal_digest(q.creator_leaf) not in registration.creator_leaf_digests
        or intent.signing_wallet not in registration.signing_wallets
        or intent.purpose not in registration.purposes
        or intent.purpose is FinancialEffectPurpose.AMENDMENT
    ):
        raise FinancialTransitionError("funding actor cannot perform this leaf/wallet/effect")
    if any(
        e.intent.effect_id == intent.effect_id or e.intent.attempt_id == intent.attempt_id
        for e in state.effects
    ):
        raise FinancialTransitionError("retained effect cannot be signed again; reconcile only")
    if not state.effects and intent.purpose is not FinancialEffectPurpose.CREATE:
        raise FinancialTransitionError(
            "initial deployment mapping requires its actual create intent"
        )
    if state.effects and intent.purpose is FinancialEffectPurpose.CREATE:
        raise FinancialTransitionError("replacement create needs a separately qualified contract")
    if state.effects and state.effects[0].state is not FinancialEffectState.CONFIRMED:
        raise FinancialTransitionError("funding requires a confirmed original create mapping")
    return FinancialOperationState(
        q,
        state.revision + 1,
        canonical_journal_digest(state),
        (*state.effects, FinancialEffectRecord(intent, ())),
    )


def propose_financial_observation(
    state: FinancialOperationState,
    effect_id: str,
    observation: FinancialEffectObservation,
    commit: FinancialCommitBinding,
    *,
    now: int,
) -> FinancialOperationState:
    """Append claimed evidence after authentication by the registered adapter.

    This proposal neither sends a transaction nor releases the owner's full
    lifetime allocation. In particular, a broadcast-claim record is not send
    authority: signing/broadcasting still need durable one-use CAS and ACK.
    """

    _time(now)
    validate_financial_commit_binding(state, commit)
    if commit.observed_at > now:
        raise FinancialTransitionError("financial ACK data is from the future")
    if type(observation) is not FinancialEffectObservation or observation.observed_at > now:
        raise FinancialTransitionError("financial observation is future or malformed")
    if observation.state is FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN:
        principal = observation.principal_observation
        if (
            observation.observed_at != now
            or type(principal) is not ServicePrincipalObservation
            or principal.status is not PermitRevocationStatus.ACTIVE
            or not principal.observed_at <= now <= principal.valid_until
        ):
            raise FinancialTransitionError("new broadcast claim requires current principal data")
    _text(effect_id, "effect identity")
    matches = tuple(e for e in state.effects if e.intent.effect_id == effect_id)
    if len(matches) != 1:
        raise FinancialTransitionError("exact retained financial effect is missing")
    current = matches[0]
    try:
        updated = replace(current, observations=(*current.observations, observation))
    except ValueError as exc:
        raise FinancialTransitionError("financial observation skips or rewrites history") from exc
    return FinancialOperationState(
        state.maximum,
        state.revision + 1,
        canonical_journal_digest(state),
        tuple(updated if e is current else e for e in state.effects),
    )
