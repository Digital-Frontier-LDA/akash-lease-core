# Akash lifecycle interoperability v1

This directory is a language-neutral artifact view of the broker admission and durable
capacity-reservation state machines shipped by `akash-lease-core` 0.12.0. It contains no I/O
adapter and grants no network authority.

`bundle.json` binds every artifact path to its exact canonical SHA-256 digest. `model.json` is a
closed structural catalog: it lists every supported enum, record, required field, union and
sequence shape, plus the allowed wire and storage roots. Semantic constraints remain enforced by
the v0.12 core and measured vectors; the catalog does not claim to replace them.
`canonicalization.json` defines the byte profile. `vectors.json` pins three reservation outcomes,
eight permit-confirmation outcomes, one control/astral/key-order corpus, and six integer-boundary
cases against the Python model and an independently written Node adapter. The integer cases include
`2^53-1`, `2^53`, `2^64-1`, a 41-digit value, a leading zero, and a negative value; the Node adapter
parses JSON integers losslessly rather than narrowing the Python core's unbounded counters to
IEEE-754.

The catalog gives consumers complete serialization shapes, including roots that have no behavioral
vector in this release. Behavioral coverage is narrower: the vectors exercise admission requests,
reservation state/proposals, persistence confirmations, and the permit-issuance decision. They do
not exercise a serialized `AdmissionResult` or `CreatePermit`, or permit presentation, redemption,
and submission-authorization behavior. Those types are structural schema in v1, not measured
cross-language behavior.

The key-order corpus includes U+E000 and U+10000. Python code-point order places U+E000 first;
JavaScript's default UTF-16 `.sort()` reverses them, so passing the vector requires an explicit
code-point comparator. All JSON strings must contain Unicode scalar values; lone surrogate code
points are rejected before canonicalization.

The bundle deliberately excludes every wire/storage role that v0.12 does not define, including a
broker error envelope, idempotency envelope, atomic transaction membership, outcome and
reconciliation writes, close authorization, chain-finality observation, and settlement.
`coverage.json` enumerates the complete normative role population as supported or
`unavailable_fail_closed`; a consumer must not infer a missing role from a nearby Python type.

Build or verify the committed bytes with:

```console
python tools/build_interoperability_bundle.py --check
node tools/verify_interoperability_bundle.mjs
```

An external standard locator must bind `bundle.json` after merge by recording its repository,
40-character merge commit, path, and exact SHA-256 digest. That is deliberately a second-stage
index: this repository requires squash merges, so a commit named from inside its own pull request
would be orphaned, while naming the future merge commit would be self-referential.
