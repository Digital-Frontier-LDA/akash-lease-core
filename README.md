# akash-lease-core

Sans-I/O core for Akash **wallet, lease acquisition, deployment identity, workload identity, and
lease-shell semantics**: deterministic wallet ranking, a deadline-bound
provider auction, canonical owner/DSEQ and deployment-group identities, frame codec, URL
builders, and trustworthy exec result interpretation.

No sockets. No event loop. No `ssl`, `websockets`, `requests`, or `httpx`. Stdlib only, **zero runtime dependencies**.

```bash
pip install "git+https://github.com/Digital-Frontier-LDA/akash-lease-core@v0.7.0"
```

## Why

`exit_code == 0` is **not** a trustworthy success signal for a lease-shell exec. It occurs with empty stdout in at least two real, observed cases:

- **(A) transient stdout-teardown drop** — a fast-exiting command's trailing stdout is lost as the SPDY/CRI stream half-closes.
- **(B) closed-lease fake success** — exec against a dead lease returns a *synthetic* `{"exit_code": 0}` with no output and **no failure frame**.

Both return `rc=0` while nothing useful came back, so a bare rc-check **false-passes a broken provider**. The remedy is marker-echo (require an echoed token in stdout) or `require_stdout`:

```python
from akash_lease_core import interpret_success

interpret_success(0, "")                          # True  — legacy rc-trust (no-output cmds)
interpret_success(0, "", marker="TOKEN")          # False — closed lease / dropped stdout
interpret_success(0, "TOKEN\n", marker="TOKEN")   # True  — verified
interpret_success(0, "", require_stdout=True)     # False — expected output, got none
```

## The standard this implements

This library decides things the **Akash runner standard** mandates — provider qualification,
the auction, funding. It does not define that standard, and there is no copy of it here on
purpose: a third copy is the failure centralising the code was meant to remove.

| document | normative for |
|---|---|
| **df-wiki** `content/platform/akash-github-runners.md` | the mandates **§1–§11** — the only doc with `## N` sections |
| **df-cicd** `standards/AKASH-RUNNER-CI.md` | the **CI contract** and workflow template |
| **akash-github-runner** `akash_runner/check_*.py` | the **rules that enforce** both |

⇒ For a §-numbered mandate, read **df-wiki**. For the CI contract, read **df-cicd**.

### Consumer versions — measured 2026-09-11

Adoption of the *code* is complete: across just-akash (18 importers), Blazing-Back (14) and
akash-github-runner (3) there are **zero local re-definitions** of `qualified_set`,
`evaluate_provider`, `from_provider_status` or `PreferredSelection`. Nobody has forked the
logic.

The effective consumer pins still predate the identity contracts:

```text
akash-lease-core main   0.11.1
Blazing-Back            v0.9.0   control-plane/api/requirements.txt:88
                        v0.9.0   control-plane/workers/requirements.txt:73
just-akash              v0.9.0   uv.lock (resolved)
```

⚠ **Count only what INSTALLS.** A grep for this package across Blazing-Back also returns
`0.7.0`, `0.8.0` and `0.2.0` — every one of them in a comment, a planning document, or a test
docstring discussing older behaviour. #33 read those as live pins and reported a three-version
range inside one consumer; the resolved skew is a single version. A naive grep counts prose as
configuration, and here it inflated the finding by two versions.

⛔ **This is a record, not an adoption instruction.** Whether `0.9.0 → 0.11.1` contains behaviour
changes that matter has not been determined. Upgrading consumers onto a version nobody has
diffed is how a shared library becomes an incident — establish the intended pin contract
(#32) first.

## Design: sans-I/O

The protocol is a pure function of bytes; **each consumer supplies its own I/O adapter** — a blocking transport for a CLI, an async one for a service. That is what lets one implementation serve both without a shared event-loop assumption. See [sans-io.readthedocs.io](https://sans-io.readthedocs.io/).

```
        akash-lease-core  (pure: frames, URLs, result semantics)
           ▲                        ▲
   blocking adapter          async adapter
   (CLI, Console proxy)      (service, direct-to-provider)
```

## Frame protocol

| code | meaning |
|-----:|---------|
| 100 | stdout |
| 101 | stderr |
| 102 | result — JSON `{"exit_code": N}` **or** a raw 4-byte LE int32 |
| 103 | failure |
| 104 | stdin |
| 105 | resize |

## Two wire paths

- **direct-to-provider** — `wss://{provider}/lease/{dseq}/{gseq}/{oseq}/shell` (`build_direct_provider_ws_url`)
- **Console provider-proxy** — relays frames in a JSON envelope with base64 payloads (`build_proxy_connect_message`, `decode_proxy_payload`)

Both share the frame codec and result semantics. The divergence is egress strategy, which stays in the adapter.

## Provider auction

`Auction` is a clock-neutral state machine shared by Console, wallet/chain, and
CLI adapters. Adapters normalize external bids and supply their own monotonic
clock; the core performs no polling or networking.

```python
from decimal import Decimal

from akash_lease_core import Auction, AuctionPolicy, BidObservation

auction = Auction(
    AuctionPolicy(
        collection_window_seconds=60,
        preferred_providers=frozenset({"akash1lisbon", "akash1sofia"}),
        eligible_providers=frozenset({"akash1lisbon", "akash1sofia", "akash1fallback"}),
    ),
    started_at=0,
)
auction.observe(
    BidObservation(
        bid_key="order/provider/gseq/oseq",
        provider="akash1lisbon",
        price=Decimal("4.2"),
        denom="uact",
        observed_at=58,
    )
)

assert auction.evaluate(now=59).status.value == "collecting"
decision = auction.evaluate(now=60)
assert decision.selected.provider == "akash1lisbon"
```

The invariant is: collect for the complete configured window (0–60 seconds),
then choose the cheapest open preferred bid. If none exists, enter a bounded
fallback phase and select the first observed open eligible bid; a fallback that
already bid can be selected immediately at the phase transition. Provider
eligibility is policy input—not hard-coded in this package. Mixed denominations
fail closed because unlike currencies cannot be compared safely.

### Crash resume

`Auction.snapshot()` returns a plain, JSON-native `dict`; `Auction.restore()`
rebuilds the auction from it. An adapter that dies mid-window resumes with the
arrival times it already collected instead of re-dating every surviving bid to
the restart -- which would hand the fallback rule a pool that all arrived at
once.

```python
blob = auction.snapshot(scope="dseq:24680")        # store it however you like
resumed = Auction.restore(blob, expect_scope="dseq:24680")
assert resumed.evaluate(now=60) == auction.evaluate(now=60)
```

It refuses rather than guesses. An unknown schema version raises
`UnsupportedSnapshotVersion`; a field set that does not match the dataclass, a
price that is not a string, a duplicate `bid_key` and a `scope` mismatch all
raise. `observed_at` is relative to `started_at`, so a snapshot whose
`started_at` is not `0` is refused unless `restore(..., rebase_started_at=...)`
re-anchors it explicitly: `time.monotonic()`'s reference point is undefined
across processes, and on Linux it is *coincidentally* meaningful on the same
host -- which is worse, because it makes a same-host restart test pass. Persist
a wall-clock anchor of your own beside the blob and compute
`now = (utcnow() - anchor).total_seconds()` on resume.

`scope` is opaque here and the core never reads it. It exists because `bid_key`
is unique WITHIN an order and not across the chain, so a snapshot handed back by
a lookup that was wrong would merge two deployments' bids into one auction --
which `observe()` cannot detect, since it raises only when a key changes
provider.

## Console wallet ranking

`rank_wallets` receives non-secret account snapshots and returns a deterministic
attempt order: unique accounts with enough credit, richest first. Adapters retain
all secret handling and I/O—including account discovery, authoritative allowance
reads, DSEQ-owner lookup, and cross-process coordination.

Two keys resolving to one account are one source of sequence and funding capacity,
not two. The core therefore folds duplicate accounts before ranking. Consumers must
route later status/update/destroy operations to the account that owns the DSEQ;
re-running the richest-wallet rule during cleanup is unsafe because balances can
change after creation.

## Workload identity

`DeploymentKey` keeps the chain owner and DSEQ together as the identity used by
exact deployment reads and destructive operations. It rejects owner strings that
are not lowercase, checksum-valid, 20-byte `akash` Bech32 accounts and DSEQs that
are not canonical positive Akash uint64 strings. A caller must not validate the
two fields separately and later pass a bare DSEQ across an authorization boundary.

`format_identity`, `parse_identity`, `classify_groups`, and `transform_sdl`
implement the versioned `idv1` group-name contract shared by Akash producers
and cleanup readers. Callers supply an explicit prefix-to-repository ownership
register; the core never infers ownership from live deployments. The SDL
transformer atomically renames every placement definition and corresponding
deployment reference, or rejects the whole document.

Every observed group in a deployment must describe one lifecycle, encode its
actual group number, and form the complete canonical `1..N` population. A
legacy name, malformed field, duplicate group, mixed class, lifecycle
disagreement, or unverified population returns a held `Population`. The caller
must supply typed completeness evidence; held results retain parsed identities
and population counts for diagnosis. A successful parse is attribution, not
permission to retire the deployment: current CI run state, expiry, or explicit
production retirement authority remains a separate input.

```python
from dataclasses import replace

from akash_lease_core import (
    GroupObservation,
    Identity,
    PopulationCompleteness,
    classify_groups,
    format_identity,
)

owners = {"example": "example-org/example-repo"}
first = Identity(
    prefix="example",
    owner="example-org/example-repo",
    workload_class="ci-runner",
    group=1,
    run=12345,
    attempt=2,
)
second = replace(first, group=2)
names = [format_identity(item, owners) for item in (first, second)]
observations = [GroupObservation(group, name) for group, name in enumerate(names, start=1)]

assert classify_groups(
    observations,
    owners,
    completeness=PopulationCompleteness.COMPLETE,
).held is False
```

## Durable create-journal contract

`CreateJournal` is the immutable, sans-I/O state machine for one or more create
operations. A `PreparedCreate` binds the authenticated producer, lifecycle,
authoritatively resolved owner candidate, exact request and SDL digests, source
revision, and complete ordered `(gseq, group_name)` population before a create
side effect. `SubmissionEvidence` then records either the exact signed bytes,
transaction hash and account sequence for a self-managed backend, or a
server-issued submission token for a mediated backend.

`CreateOutcome` keeps `rejected_before_send`, `fallback_safe`, `committed`, and
`unknown` distinct with stable `OutcomeReasonCode` values. Broad exceptions,
process and HTTP timeouts, response loss, and backend or provider rotation are
always unknown. A deployment reaches `created` only when a transaction event or
exact chain read binds its canonical owner/DSEQ and complete group digest to the
prepared operation. A copied owner/DSEQ pair is therefore not binding evidence.

Every entry includes its predecessor digest and its own canonical digest.
`CreateJournal.append` returns a new journal and revalidates the full history,
including operation uniqueness, permitted state edges, immutable prepared
fields, one deployment per operation, one operation per deployment, and
monotonic settlement. The package deliberately supplies no database, broker,
filesystem, clock, chain reader, or consumer integration; adapters must provide
atomic durable create/append and reread behavior around this contract.

## Close authorization policy

`evaluate_close` is a pure decision boundary. It never closes a deployment and
it never treats a successfully parsed group name as close authority. Every
decision binds an exact `DeploymentKey` to a complete workload `Population`, an
explicit `CloseIntent`, current lifecycle authority, authenticated producer
evidence, and fresh observations from two agreeing chain readers.

The intent variants are deliberately separate: CI cleanup, staging retirement,
creator rollback, and production retirement. CI authority is
denied for staging and production payloads. Production retirement requires its
own typed authority. That authority binds one subject, release, environment,
action, and single-use authorization to authenticated requester, approver,
and executor principals. The approver is a human operator. The executor is a
distinct machine principal proven to be neither an admin nor an approver. A
human operator may request and approve the same action; an automated requester
cannot approve itself. This preserves single-developer operation without
letting a human approval become the executing credential.

GitHub Environment evidence records both self-review and admin-bypass settings
instead of assuming one configuration. Disabled self-review prevention is
accepted for a human-originated request or a distinct authenticated requester.
Enabled admin bypass requires an explicit policy and non-admin automation. If
bypass was used, a source-bound event must identify the authorized human
approver; unknown or automated actors deny. The authority also binds the
approved workflow commit and ref. External approval sources carry no GitHub
environment bypass evidence and must provide equivalent separation,
uniqueness, and verification guarantees. A generic
`force` input does not exist. Unknown, incomplete, malformed, or mixed group
populations are held.
Every decision receives a deterministic `evaluated_at` value and checks it
against the observation and validity intervals on chain, authority, signer, and
attestation-verification evidence. Out-of-window evidence is held. Callers also
report how many deployments matched the lifecycle
attribution; zero or multiple candidates are held even when each candidate's
group names parse correctly.

Pre-close evidence uses `EXACT_FINALIZED_HEIGHT`: both independent trust paths
must observe the same finalized height. It carries explicit deployment state,
group count/digest, and lease count/digest/completeness. A zero-lease population
is accepted only as `EXACT_NEVER_LEASED` with the canonical empty-population
digest; an ordinary complete claim with zero matches is vacuous and held.

Producer authentication has two forms. `IsolatedSignerEvidence` records a
fresh, source-bound signer read for one chain owner, repository, workload class,
and trust domain. For shared signers, `CreationAttestation` is deliberately
neutral claim data. `AttestationVerification` separately binds the exact claim
digest to a pinned deployment-broker trust root, verified issuer and audience,
signature outcome, operation-key uniqueness outcome, and validity interval.
OIDC authenticates the producer to that broker; the broker attests the returned
owner and DSEQ after creation. The attestation retains the producer token's
issuer, audience, subject, and replay-checked `jti`, plus GitHub's stable
repository IDs, branch/tag `ref`, `workflow_ref` with its separate
`workflow_sha`, and optional-together `job_workflow_ref`/`job_workflow_sha` for
reusable-workflow jobs. The core checks these bindings but performs no OIDC,
signature, journal, or clock I/O.

```python
from akash_lease_core import (
    CIAuthority,
    CandidateCompleteness,
    CandidatePopulation,
    ChainEvidence,
    ChainProofMode,
    CloseDisposition,
    CloseIntent,
    ConsumerState,
    LeasePopulationCompleteness,
    IsolatedSignerEvidence,
    PreCloseDeploymentState,
    RunState,
    SourceAgreement,
    UniquenessStatus,
    canonical_prepared_operation_key,
    canonical_deployment_population_digest,
    canonical_group_identity_digest,
    evaluate_close,
)

decision = evaluate_close(
    subject=deployment_key,
    population=population,
    intent=CloseIntent.CI_CLEANUP,
    authority=CIAuthority(
        subject=deployment_key,
        repository="example-org/example-repo",
        run=12345,
        attempt=2,
        run_state=RunState.TERMINAL,
        consumer_state=ConsumerState.FINISHED,
        source="github-api:example-org/example-repo",
        observation_digest="github-run-and-consumers-digest",
        observed_at=1757500000,
        valid_until=1757500060,
    ),
    chain_evidence=ChainEvidence(
        subject=deployment_key,
        group_identity_digest=canonical_group_identity_digest(population),
        evidence_digest="two-source-preclose-digest",
        chain_id="akashnet-2",
        source_a="rpc-a.example",
        source_b="rpc-b.example",
        trust_path_a="operator-a/root-1",
        trust_path_b="operator-b/root-2",
        source_a_height=20000000,
        source_b_height=20000000,
        common_finality_height=20000000,
        proof_mode=ChainProofMode.EXACT_FINALIZED_HEIGHT,
        deployment_count=1,
        deployment_population_digest=canonical_deployment_population_digest(deployment_key),
        deployment_state=PreCloseDeploymentState.ACTIVE,
        deployment_state_digest="active-deployment-state-digest",
        group_count=population.observed_count,
        group_population_digest=canonical_group_identity_digest(population),
        group_state_digest="complete-group-state-digest",
        lease_count=2,
        lease_population_digest="two-complete-leases-digest",
        lease_state_digest="complete-lease-state-digest",
        lease_completeness=LeasePopulationCompleteness.COMPLETE,
        observed_at=1757500000,
        valid_until=1757500060,
        agreement=SourceAgreement.AGREEING,
    ),
    producer_authentication=IsolatedSignerEvidence(
        signer_owner=deployment_key.owner,
        repository="example-org/example-repo",
        workload_class="ci-runner",
        operation_id="create-op-123",
        environment=None,
        trust_domain="example-org/example-repo:ci-signer",
        source="backend-owner-read",
        evidence_digest="backend-owner-evidence-digest",
        observed_at=1757500000,
        valid_until=1757500060,
    ),
    candidates=CandidatePopulation(
        subject=deployment_key,
        group_identity_digest=canonical_group_identity_digest(population),
        operation_id="create-op-123",
        operation_ordinal=1,
        count=1,
        completeness=CandidateCompleteness.COMPLETE,
        prepared_operation_key=canonical_prepared_operation_key(
            signer_owner=deployment_key.owner,
            producer_repository="example-org/example-repo",
            workload_class="ci-runner",
            operation_id="create-op-123",
            operation_ordinal=1,
            prepared_group_identity_digest=canonical_group_identity_digest(population),
            run=12345,
            run_attempt=2,
        ),
        uniqueness_status=UniquenessStatus.UNIQUE,
        evidence_digest="candidate-journal-digest",
        creation_authorization_reference=None,
    ),
    evaluated_at=1757500000,
)
assert decision.disposition is CloseDisposition.ALLOW
```

Reviewed legacy retirement is intentionally absent from the public v0.12 API.
Unknown and legacy identity populations hold; exposing an authority variant
that can never allow would invite consumers to mistake representation for a
working retirement path.

Creator rollback is the narrow exception to two-source pre-close reads. Its
capability is prepared before creation, then must gain a positive same-operation
binding from the create transaction/event or an exact complete chain readback
whose group digest equals the prepared digest. A response DSEQ by itself is not
enough on a shared owner. The capability also binds the repository, complete
lifecycle identity, and original payload creation authorization. Fresh,
source-bound handoff evidence must say `FAILED`; `SUCCEEDED`, `UNKNOWN`, stale,
or unreadable trigger evidence denies compensation. This permits immediate
compensation after a proven failed handoff without converting a later sweeper
into creator authority or allowing it to close a healthy deployment.

Version 1 workload names intentionally remain unchanged. They do not encode a
create ordinal, so a retry or redeploy can create multiple deployments with the
same repository/class/run/attempt identity. A complete `CandidatePopulation`
and journal-backed prepared-operation key close that ambiguity at policy time. A future
on-chain identity schema can add an ordinal without silently changing the
established `idv1` wire format.

This contract authorizes an attempted close; it does not claim the outcome.
`ExecutionClosure` records the separate post-close proof. Both observations
must name distinct endpoints, operators, and trust paths whose operational
independence the adapter verified. They must identify one chain and one exact
finalized height at or after the close transaction height. The core also keeps
execution closure separate from `settled`, `overdrawn_unsettled`, and `unknown`
settlement evidence: deployment/groups/leases may be terminal while escrow is
still unsettled. Adapters remain responsible for authenticating the recorded
operator identities, determining finality, and obtaining complete populations;
the sans-I/O contract refuses to persist an untyped or internally inconsistent
claim as `execution_closed`.

## Reconciled semantics

This package unifies two prior implementations that had **drifted**. Rather than silently imposing one, both behaviours are explicit:

| Case | `strict=False` (default) | `strict=True` |
|---|---|---|
| malformed / non-JSON payload | returns `default` (`-1`) | raises `MalformedResultFrame` |
| JSON without `exit_code` | returns `default` | raises |
| `exit_code` not an int (incl. `bool`) | returns `default` | raises |

Use `strict=False` for a service that must never raise; `strict=True` for a CLI where a corrupt frame is a real defect that should be loud.

## Invariants

1. **Zero runtime dependencies.** `dependencies = []` is deliberate; a test asserts the module imports no networking library.
2. **No I/O.** If you need a socket, a sleep, or a clock read, you are writing an adapter, not core.
3. **Python >= 3.10**, so both consumers can adopt it.

## License

MIT
