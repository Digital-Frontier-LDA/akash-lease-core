# Durable operation exposure data contract

This is additive, sans-I/O financial schema version 1. It adds no package
version, production issuer, policy default, networking, persistence or activation.
Existing 0.16.1 journal, admission and recovery models/bytes stay unchanged.

## Two phases and authority

1. A registered adapter obtains an **authenticated finite maximum proposal**
   before owner reservation. `OperationMaximumLiability` binds the original
   `PreparedCreate`, exact creator leaf, native cap vector, backend, policy,
   complete writer population and common CAS domain. Its constructor validates
   shape and internal equality. It does not authenticate the proposal.
2. The broker authenticates that proposal and complete owner/leaf census, reserves
   the full maximum once under the existing owner/leaf budget, and atomically
   commits the financial pointer with that same owner head. Independent verification
   of the signed ACK is required before an effective quote or one-use signing
   authorization can exist. `FinancialCommitBinding` is only retained ACK **data**;
   `validate_financial_commit_binding` compares values and returns `None`.

All `propose_*` functions return financial **state proposals**, never permission
to sign or send. A fabricated typed record, checksum, constructor success or
decoded record cannot establish policy, issuer, deployment, chain truth or CAS.
This slice intentionally supplies no `FinancialSignAuthorization` constructor.
The broker/signer phases must implement actual one-use authority at their lowest
instrument boundaries after authenticating the durable commit.

## Principal and original leaf

`ServicePrincipalBinding` identifies a genuine service issuer, subject, audience,
credential fingerprint, deployed source and registration. A current
`ServicePrincipalObservation` binds authentication evidence, revocation epoch and
window. `FundingActorRegistration` binds its permitted wallets/purposes/leaves to
the exact chain, owner, backend, source policy, complete writer population and CAS
domain. Current observations must be authenticated by a registered adapter.

The delegated funder is an actuator charging the **original creator leaf**. It
does not manufacture a GitHub identity, become another ownership leaf, create
another active/unresolved reservation, or add the maximum again. This initial
contract retains the existing `PreparedCreate` and `AdmissionScope`; an independent
non-GitHub resource creator requires an additional faithful creation-principal
contract. Merely registering a funder does not represent every wallet writer.

## Native amounts and financial basis

Vectors contain sorted, unique, exactly preserved native denomination names and
nonnegative arbitrary precision integers. No denomination whitelist, alias,
exchange rate or currency equivalence is inferred. Encoding is bounded to 256
UTF-8 bytes per name; control characters and surrounding whitespace are rejected.

`QualifiedUactBasis` carries a claimed authenticated attribution policy and must
bind the **exact** native maximum and policy revision. It is optional: a native
vector alone cannot supply existing `CreateExposureEvidence.amount_uact`.
The adapter must qualify the economic meaning, including fees, contingent grants,
mint/conversion/deposit overlap and every same-wallet liability. Adding vectors
is valid only after amounts have already been attributed under that policy.
The core does not turn credits, runtime hints or deposits into lifetime exposure.

The stored codec uses canonical integer JSON tokens with arbitrary precision.
Consumers must parse quantities without IEEE-754 `Number` coercion. A future
TypeScript wire adapter may use an explicitly versioned decimal-string encoding;
it must not silently change these canonical stored bytes or signed digests.

## Retained transitions

An initial effect proposal retains `SIGN_RESERVED_UNKNOWN` **before signing**.
Every effect ID, attempt ID, exact signing document and wallet/account sequence
is permanent within the operation. A duplicate is reconciliation-only.

| Current state | Permitted next evidence |
| --- | --- |
| `SIGN_RESERVED_UNKNOWN` | `SIGNED` or `PROVEN_NOT_SIGNED` |
| `SIGNED` | `BROADCAST_CLAIMED_UNKNOWN` |
| `BROADCAST_CLAIMED_UNKNOWN` | `CONFIRMED` or `FAILED` |
| Any terminal state | None |

Evidence must retain the original intent and transaction hash. A broadcast claim
requires exact, current, active principal data. It is still only a proposal;
the real instrument needs current policy/revocation and a durable one-use claim
ACK. No state read or old valid ACK permits another sign/send from a new process.
New broadcast-claim proposals must be dated at the current `now`, and principal
evidence must remain valid at that time. Backdating a new claim cannot revive old
principal evidence. Decoding an already-retained historical claim continues to
validate its original evidence window without treating it as current authority.

Unknown/inflight effects consume their full proposed debit. Confirmed or failed
effects retain the authenticated actual debit, within that maximum and covering
the declared native fees. A failed transaction is not zero spend.
`PROVEN_NOT_SIGNED` requires independently authenticated evidence from the adapter;
an exception, transaction lookup absence or missing private signed bytes does not
establish it. The identity/tombstone remains after that proof.

Funding requires the confirmed original create mapping and the same deployment.
The first slice holds replacement creates and cap amendments. It exposes no
operational-close, reset or settlement transition to free the owner's maximum.
Proposal expiry governs initial acquisition; later authorized funding consumes
the same retained lifetime allocation with fresh actor/policy evidence. Day
rollover, restart, revocation or policy rotation does not erase historical charges.

## Recovery and integration requirements

`financial_codec` has an independent closed data allowlist, exact fields/type tags,
duplicate-key rejection, strict primitive types, canonical byte equality and
8 MiB / 64-depth / 100,000-node bounds. Decoding is data recovery only. The legacy
recovery registry is unchanged and rejects these financial roots.

The next broker source phase must persist full retained financial bytes/digest and
revision in **the same owner-head CAS and independently verified signed ACK** as
the original owner/leaf admission reservation. Every enrolled writer must honor
the financial pointer; a legacy writer that ignores it must fail closed. Separate
Console counters cannot provide atomic owner capacity across Guardian/Accounting.

The next Console source phase must propagate actual operation identity through
manual/scheduled deposits, reload, grants and fees; decode all actual effects;
reserve UNKNOWN immediately before signing after fees/account/sequence are known;
persist exact public transaction hash and a one-use send claim before broadcast;
and hold automatic replacement/retry after an unknown outcome. Network/key effects
must never execute inside a retryable DB transaction. Cross-operation/wallet batch
authority must be atomic for all affected keys or that guarded batch must hold.

Source fixtures establish none of the required approved numeric policy, actual
issuer/quote acquisition, denomination attribution, all deployed signer/key/grantee
custody, safe existing-state migration or live production enforcement. Updating a
fork cannot constrain an existing deployed signer outside that gate. No original
#1063 financial or full goal acceptance is claimed by this pure slice.
