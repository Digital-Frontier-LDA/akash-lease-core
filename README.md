# akash-lease-core

Sans-I/O core for the **Akash provider lease-shell** wire protocol: frame codec, URL builders, and — the reason this package exists — **trustworthy exec result semantics**.

No sockets. No event loop. No `ssl`, `websockets`, `requests`, or `httpx`. Stdlib only, **zero runtime dependencies**.

```bash
pip install "git+https://github.com/Digital-Frontier-LDA/akash-lease-core@v0.1.0"
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
2. **No I/O.** If you need a socket, you are writing an adapter, not core.
3. **Python >= 3.10**, so both consumers can adopt it.

## License

MIT
