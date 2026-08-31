# akash-lease-core

Sans-I/O core for Akash **wallet, lease acquisition, and lease-shell semantics**:
deterministic wallet ranking, a deadline-bound provider auction, frame codec, URL
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

### Consumer versions — measured 2026-08-31

Adoption of the *code* is complete: across just-akash (18 importers), Blazing-Back (14) and
akash-github-runner (3) there are **zero local re-definitions** of `qualified_set`,
`evaluate_provider`, `from_provider_status` or `PreferredSelection`. Nobody has forked the
logic.

The *version* is one release behind in both consumers:

```text
akash-lease-core main   0.10.0
Blazing-Back            v0.9.0   control-plane/api/requirements.txt:88
                        v0.9.0   control-plane/workers/requirements.txt:73
just-akash              v0.9.0   uv.lock (resolved)
```

⚠ **Count only what INSTALLS.** A grep for this package across Blazing-Back also returns
`0.7.0`, `0.8.0` and `0.2.0` — every one of them in a comment, a planning document, or a test
docstring discussing older behaviour. #33 read those as live pins and reported a three-version
range inside one consumer; the resolved skew is a single version. A naive grep counts prose as
configuration, and here it inflated the finding by two versions.

⛔ **This is a record, not a plan to bump.** Whether `0.9.0 → 0.10.0` contains behaviour
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
