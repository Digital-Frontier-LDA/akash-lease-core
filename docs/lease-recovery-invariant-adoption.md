# Lease-recovery invariant adoption record

Status: **staged library contract; adoption required before declaring this behavior shipped**.

The pure decision table lives in `akash_lease_core.lease_recovery` and is intended
for these consumers:

- the Akash runner provisioner (pre/post-strike readiness handling),
- deployment closers and scheduled sweepers, and
- future reapers governed by the shared lifecycle contract (#1552).

This PR intentionally has no transport consumer: the package is sans-I/O and the
three callers have different HTTP/authentication adapters. The consuming-repository
work must import `evaluate_lease_recovery` and test the mapping from its observed
lease/readiness/quota states to the caller's wait/fail/close behavior. Until those
callers land, this artifact is **not** evidence that production behavior changed;
it is the owned adoption checkpoint for the contract.
