"""Sans-I/O core for the Akash provider lease-shell wire protocol.

Pure functions over bytes/text — **NO sockets, NO event loop, NO ssl, NO
websockets**. Every consumer supplies its own I/O adapter: a blocking transport
for a CLI, an async one for a service. See README "Invariants".

Frame protocol (provider <-> client) — one code byte followed by the payload:

    100 stdout   101 stderr   102 result   103 failure   104 stdin   105 resize

Two wire paths reach the same provider endpoint and share this codec:

* **direct-to-provider** — ``wss://{provider}/lease/{dseq}/{gseq}/{oseq}/shell``
  (used by a control plane that can reach providers directly).
* **Console provider-proxy** — ``wss://console.akash.network/provider-proxy-*``,
  which relays frames inside a JSON envelope with a base64 payload (used by a
  CLI that wants simple egress).

Result-success semantics (the reason this package exists)
---------------------------------------------------------
A provider ``exit_code`` of 0 is **not** a trustworthy success signal for
lease-shell exec. It occurs with empty stdout in at least two real cases:

  (A) a transient SPDY/CRI stdout-teardown drop of a fast-exiting command's
      trailing stdout, and
  (B) exec against a **closed/dead lease**, which returns a *synthetic*
      ``{"exit_code": 0}`` with no output and no failure frame.

Callers that need a trustworthy verdict must supply a ``marker`` (require the
echoed token in stdout) or set ``require_stdout``.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import urllib.parse

__all__ = [
    "STDOUT",
    "STDERR",
    "RESULT",
    "FAILURE",
    "STDIN",
    "RESIZE",
    "MAX_URL_CMD_BYTES",
    "MalformedResultFrame",
    "decode_frame",
    "parse_result_exit_code",
    "command_needs_stdin_delivery",
    "build_direct_provider_ws_url",
    "build_proxy_connect_message",
    "decode_proxy_payload",
    "is_unverified_success",
    "interpret_success",
]

__version__ = "0.2.0"

# ---------------------------------------------------------------------------
# Binary frame protocol constants
# ---------------------------------------------------------------------------
STDOUT = 100
STDERR = 101
RESULT = 102
FAILURE = 103
STDIN = 104
RESIZE = 105

# Commands whose URL-encoded form exceeds this are delivered over STDIN (code
# 104) instead of a ``cmd2`` query param, to stay under practical HTTP URL
# length limits (~8 KB).
MAX_URL_CMD_BYTES = 4096


class MalformedResultFrame(ValueError):
    """A RESULT(102) payload could not be parsed into an exit code."""


def _join(command: str | list[str]) -> str:
    return " ".join(command) if isinstance(command, list) else command


# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------
def decode_frame(msg: object) -> tuple[int, bytes] | None:
    """Split a raw binary frame into ``(code, payload)``.

    Returns ``None`` for anything that is not a valid frame (non-bytes, or an
    empty message), so callers can simply skip it.
    """
    if not isinstance(msg, (bytes, bytearray)) or len(msg) < 1:
        return None
    return msg[0], bytes(msg[1:])


def parse_result_exit_code(payload: bytes) -> int:
    """Parse a RESULT(102) payload into an exit code, or raise.

    Accepts the two encodings the protocol actually uses:

    * a JSON object — ``{"exit_code": N}`` where N is a real int
    * a raw **exactly** 4-byte little-endian **signed** int32

    Everything else raises :class:`MalformedResultFrame`. There is deliberately
    **one** behaviour and no compatibility flags: ``rc == 0`` is not a
    trustworthy success signal, so a frame we cannot parse must surface as an
    error rather than masquerade as ``0``. In particular a frame with a missing
    or null ``exit_code`` is corruption, NOT a success — note that the
    closed-lease failure mode emits ``{"exit_code": 0}`` with the key *present*,
    so a keyless frame is a distinct, unexplained condition.

    No ``int()`` coercion: ``{"exit_code": "7"}`` or ``true`` is a malformed
    frame, and silently coercing it turns nonsense into a plausible exit code.
    """
    try:
        parsed = json.loads(payload)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueError
        parsed = None

    if isinstance(parsed, dict):
        if "exit_code" not in parsed:
            raise MalformedResultFrame(
                f"result frame has no exit_code (a keyless frame is corruption, "
                f"not a success): {payload!r}"
            )
        code = parsed["exit_code"]
        # bool is an int subclass; an exit code is never True/False.
        if isinstance(code, bool) or not isinstance(code, int):
            raise MalformedResultFrame(f"exit_code {code!r} is not an integer")
        return code

    if parsed is not None:
        raise MalformedResultFrame(
            f"result frame JSON is a {type(parsed).__name__}, expected an object with exit_code"
        )

    # Legacy binary form: EXACTLY 4 bytes, little-endian SIGNED int32. A longer
    # non-JSON payload is malformed — reading its first 4 bytes would invent an
    # exit code (e.g. 5 NUL bytes must not become exit 0).
    if len(payload) == 4:
        return int(struct.unpack("<i", payload)[0])

    raise MalformedResultFrame(
        f"result frame: {len(payload)} byte(s), could not parse an exit code "
        '(expected JSON {"exit_code": N} or a 4-byte LE int32)'
    )


# ---------------------------------------------------------------------------
# Wire path A — direct to provider
# ---------------------------------------------------------------------------
def command_needs_stdin_delivery(command: str | list[str]) -> bool:
    """True if *command* is too large to carry in the URL and must use STDIN."""
    return len(urllib.parse.quote(_join(command), safe="")) > MAX_URL_CMD_BYTES


def build_direct_provider_ws_url(
    provider_host: str,
    dseq: str,
    gseq: str,
    oseq: str,
    service_name: str,
    command: str | list[str],
) -> str:
    """Construct the direct-to-provider lease-shell ``wss://`` URL.

    Oversized commands get an interactive ``/bin/sh`` URL (no ``cmd2``); the
    caller must then deliver the command over STDIN (code 104).
    """
    encoded_cmd = urllib.parse.quote(_join(command), safe="")
    base = f"wss://{provider_host}/lease/{dseq}/{gseq}/{oseq}/shell?stdin=1&tty=0&podIndex=0"
    if len(encoded_cmd) <= MAX_URL_CMD_BYTES:
        return f"{base}&cmd0=%2Fbin%2Fsh&cmd1=-c&cmd2={encoded_cmd}&service={service_name}"
    return f"{base}&cmd0=%2Fbin%2Fsh&service={service_name}"


# ---------------------------------------------------------------------------
# Wire path B — Console provider-proxy
# ---------------------------------------------------------------------------
def build_proxy_connect_message(
    shell_path: str,
    jwt: str,
    provider_address: str,
    stdin_data: str | None = None,
) -> dict:
    """Build the JSON envelope the Console provider-proxy expects on connect.

    Shape is dictated by the proxy, not by us — ``providerAddress`` and
    ``isBase64`` are required, and ``auth`` is ``{"type": "jwt", "token": ...}``
    (NOT ``{"jwt": ...}``). Returned as a dict so the caller decides how to
    serialise it.
    """
    msg: dict = {
        "type": "websocket",
        "url": shell_path,
        "providerAddress": provider_address,
        "auth": {"type": "jwt", "token": jwt},
        "isBase64": True,
    }
    if stdin_data is not None:
        msg["data"] = base64.b64encode(stdin_data.encode("utf-8")).decode("ascii")
    return msg


def decode_proxy_payload(data: str, *, text_fallback: bool = False) -> bytes | None:
    """Strictly base64-decode one relayed proxy payload.

    Returns ``None`` when *data* is not valid base64 and ``text_fallback`` is
    False. On the shell path a non-base64 frame is corruption and must be
    discarded rather than surfaced as output. On the logs/events path the proxy
    may relay plain text, so ``text_fallback=True`` returns it UTF-8 encoded.
    """
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        if text_fallback:
            return data.encode("utf-8")
        return None


# ---------------------------------------------------------------------------
# Result-success interpretation
# ---------------------------------------------------------------------------
def is_unverified_success(exit_code: int, stdout: str) -> bool:
    """True when ``exit_code == 0`` but stdout is empty.

    The ambiguous case ``exit_code`` alone cannot distinguish: a genuine
    no-output success, a dropped-stdout race (A), or a closed-lease synthetic
    zero (B). Treat it as "success unverified", not "success".
    """
    return exit_code == 0 and not (stdout or "").strip()


def interpret_success(
    exit_code: int,
    stdout: str,
    *,
    marker: str | None = None,
    require_stdout: bool = False,
) -> bool:
    """Return a trustworthy success verdict for a lease-shell exec.

    Precedence:

    * ``exit_code != 0``  -> ``False`` (always).
    * ``marker`` given    -> the marker must appear in stdout (marker-echo: the
      only signal that survives both failure modes A and B).
    * ``require_stdout``  -> stdout must be non-empty.
    * otherwise           -> ``exit_code == 0`` (legacy rc-trust, retained so
      callers running no-output commands — ``mkdir``, ``chmod``, secret writes —
      are not silently broken).
    """
    if exit_code != 0:
        return False
    text = stdout or ""
    if marker is not None:
        return marker in text
    if require_stdout:
        return bool(text.strip())
    return True
