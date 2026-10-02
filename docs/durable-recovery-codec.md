# Durable recovery codec v1

The external lifecycle broker needs to reload prepared operations, journals and
owner-wide reservations after a worker or host restart. Previously core exposed
only `canonical_journal_bytes`, leaving adapters to reconstruct typed objects or
accept unchecked JSON. These recovery functions validate the same version-1
storage bytes and invoke the existing model and journal history validators:

| Recovery root | Decoder |
| --- | --- |
| `CreateJournal` | `decode_create_journal` |
| `AdmissionState` | `decode_admission_state` |
| `PreparedCreate` | `decode_prepared_create` |
| `AdmissionRequest` | `decode_admission_request` |

```python
from akash_lease_core import canonical_journal_bytes, decode_create_journal

stored_bytes = canonical_journal_bytes(journal)
recovered = decode_create_journal(stored_bytes)
assert recovered.digest == journal.digest
```

Store exact canonical bytes in a binary column (or an exact text column). A
JSON/JSONB database transformation can change ordering or escape spelling and is
therefore unsuitable for this byte contract. All declared fields, including
defaults and schema versions, must be present. Unknown fields, unknown type tags,
duplicate keys, incorrect primitive types, incomplete tuples, invalid base64,
unsupported versions and noncanonical encodings raise `RecoveryDecodeError`.
Error messages and displayed exception chains omit the stored contents.

The decoder admits only a fixed set of storage data types reachable from those
four roots. It never dynamically imports a wire-supplied type. It does not decode
`CreatePermit`, `ReservationProposal`, `PermitRedemptionProposal`,
`CreateSubmissionAuthorization` or broker persistence confirmations. Stored
producer/evidence digests remain assertions that the broker must authenticate.
Shape validation and an unkeyed digest cannot prove an authorized producer or an
authentic database record; an attacker can recompute hashes.

Bounded decoding permits at most 8 MiB, 64 structural levels and 100,000 JSON
value nodes. The storage adapter must shard journals/checkpoint history before
reaching these limits using an independently verified archival protocol, or hold
admission. Silently truncating history is invalid. Existing serializers and
creation/admission policy behavior remain unchanged.

On restart, a `submitting` or `create_outcome_unknown` reservation still occupies
its active, unresolved and financial populations. Loading it does not authorize
resubmission. Repeated requests use the existing reconciliation disposition.
Execution closure releases execution capacity only; financial exposure remains
held until separate settlement evidence is committed.

This codec is a prerequisite for the external transactional broker tracked in
[just-akash #332](https://github.com/Digital-Frontier-LDA/just-akash/issues/332),
[core #39](https://github.com/Digital-Frontier-LDA/akash-lease-core/issues/39) and
[Guardian #1061](https://github.com/Digital-Frontier-LDA/guardian-cli-claude-code/issues/1061),
under [df-cicd #381](https://github.com/Digital-Frontier-LDA/df-cicd/issues/381).
It provides no database durability, atomic transaction, OIDC verification,
owner-wide census, signed permit, network create, retirement authority or
runner release qualification. Consumer migration gates must remain in force.

Compatibility is exercised with a committed prepared-operation golden vector
and the current complete journal/admission fixtures, including signed bytes,
unknown creates, closure and settlement. Further broker HTTP/storage envelopes
and cross-adapter conformance vectors remain separate required work.
