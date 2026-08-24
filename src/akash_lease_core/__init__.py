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

Read-function sentinel contract (candidate D, #17)
--------------------------------------------------
Every public read function in this package distinguishes **three** outcomes on a
single axis:

    (a) instrument failure    — could not ask         (the value ``None``)
    (b) asked, no answer      — successful empty read (the value ``EMPTY_ATTRIBUTES``)
    (c) asked, full answer    — a real, non-empty value

Only outcome (a) may be allowed to BLOCK a destructive action downstream — and
the destructive-action caller must be the one that checks for it, not the read
function itself. Conflating (a) and (b) — returning the same value for "the
instrument could not ask" and "the instrument read nothing" — silently disarms
the destructive-action gate; measured instance in a consumer: a sweeper that
closed a 200 GiB volume three times because its liveness read returned the
same value on a 404 as on a transport failure. See ``tests/test_discipline.py``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import time
import urllib.parse

from .auction import (
    Auction,
    AuctionPolicy,
    AuctionResult,
    AuctionStatus,
    BidObservation,
    MixedBidDenominations,
    RejectedBid,
)
from .wallets import (
    RejectedWallet,
    WalletCandidate,
    WalletPolicy,
    WalletSelectionResult,
    WalletSelectionStatus,
    rank_wallets,
)

__all__ = [
    "STDOUT",
    "STDERR",
    "RESULT",
    "FAILURE",
    "STDIN",
    "RESIZE",
    "MAX_URL_CMD_BYTES",
    "EMPTY_ATTRIBUTES",
    "MalformedResultFrame",
    "decode_frame",
    "parse_result_exit_code",
    "command_needs_stdin_delivery",
    "FrameTrace",
    "TRACE_ENV",
    "build_direct_provider_ws_url",
    "build_proxy_connect_message",
    "decode_proxy_payload",
    "is_unverified_success",
    "interpret_success",
    "Auction",
    "AuctionPolicy",
    "AuctionResult",
    "AuctionStatus",
    "BidObservation",
    "MixedBidDenominations",
    "RejectedBid",
    "RejectedWallet",
    "WalletCandidate",
    "WalletPolicy",
    "WalletSelectionResult",
    "WalletSelectionStatus",
    "rank_wallets",
]

__version__ = "0.7.0"

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

# Sentinel for the (b) "asked, no answer" outcome of every public read function.
# A destructive-action gate that compares against ``None`` MUST block; a value
# equal to ``EMPTY_ATTRIBUTES`` MUST NOT be conflated with ``None``. See the
# module-level "Read-function sentinel contract" docstring above. Frozen so
# identity equality is reliable across calls.
EMPTY_ATTRIBUTES: frozenset = frozenset()


class MalformedResultFrame(ValueError):
    """A RESULT(102) payload could not be parsed into an exit code."""


# Distinct sentinel for "JSON parsing failed". `None` cannot be used: b"null" is
# VALID JSON that parses to None *and* is exactly 4 bytes, so a None sentinel
# lets it fall through to the int32 branch and return a bogus 1819047278.
_JSON_PARSE_FAILED = object()


# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------
def decode_frame(msg: object) -> tuple[int, bytes] | None:
    """Split a raw binary frame into ``(code, payload)``.

    Returns ``None`` for anything that is not a valid frame (non-bytes, or an
    empty message), so callers can simply skip it.

    ⛔ CANDIDATE (D) WAS DELIBERATELY EXCLUDED HERE. ``decode_frame`` is a pure
    parser — it asks nothing, never reads from an instrument, and has no
    "asked, no answer" axis to distinguish. Its only live consumer is
    ``provider_shell_client`` (Blazing-Back), whose TWO call sites guard on
    ``if frame is None: continue`` and then unpack to ``(code, payload)``.
    Routing a successful ``b""`` websocket message to a fresh sentinel would
    fall past that guard and raise ``ValueError: not enough values to unpack``
    in the streaming loop — a runtime crash in production. The package charter
    is sans-I/O: pure parsers cannot implement the (a)/(b)/(c) split, only I/O
    ADAPTERS can. The contract candidate (D) belongs at the adapter boundary;
    see ``tests/test_discipline.py`` for the rule that does apply here.
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
        parsed = _JSON_PARSE_FAILED

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

    if parsed is not _JSON_PARSE_FAILED:
        # Valid JSON but not a result object — including the literal `null`,
        # which must NOT reach the 4-byte branch below.
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
def _argv_params(command: str | list[str]) -> list[str]:
    """The ``cmdN=`` parameters for *command*, one per argv element.

    A LIST is argv and is passed through verbatim -- ``["sh","-c","echo hi"]`` becomes
    ``cmd0=sh&cmd1=-c&cmd2=echo%20hi``. A STRING is a shell command LINE, so it keeps the
    ``/bin/sh -c`` wrapper it has always had.

    ⛔ THE LIST CASE IS THE BUG THIS REPLACES. Previously every command -- list or string
    -- was space-joined and jammed into ``cmd2`` behind a hardcoded ``cmd0=/bin/sh
    cmd1=-c``. A caller passing ``["sh","-c", script]`` therefore got

        /bin/sh -c "sh -c <script...>"

    and the inner ``sh -c`` took only the script's FIRST WORD as its command, with the
    rest becoming positional parameters. The space-join also destroyed argv boundaries
    outright, so any element containing a space was silently re-split.

    Shape ported from ``akash-network/console``, ``provider-proxy.service.ts:341``:
    one ``cmdN`` per element, no hardcoded shell.
    """
    argv = command if isinstance(command, list) else ["/bin/sh", "-c", command]
    return [f"cmd{i}={urllib.parse.quote(arg, safe='')}" for i, arg in enumerate(argv)]


def command_needs_stdin_delivery(command: str | list[str]) -> bool:
    """True if *command* is too large to carry in the URL and must use STDIN.

    Measured on the encoded ``cmdN`` payload the URL will actually carry, so a list and
    the equivalent string are judged on what they cost on the wire rather than on a
    space-joined approximation of it.
    """
    return _cmd_payload_len(command) > MAX_URL_CMD_BYTES


def _cmd_payload_len(command: str | list[str]) -> int:
    """Encoded length of the command as it rides the URL, excluding the ``cmdN=`` keys.

    For a string this is exactly the old measure (``quote(command)``), so the documented
    MAX_URL_CMD_BYTES boundary and its off-by-one tests are unchanged for that shape.
    """
    if isinstance(command, list):
        return sum(len(urllib.parse.quote(arg, safe="")) for arg in command)
    return len(urllib.parse.quote(command, safe=""))


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
    base = f"wss://{provider_host}/lease/{dseq}/{gseq}/{oseq}/shell?stdin=1&tty=0&podIndex=0"
    if _cmd_payload_len(command) <= MAX_URL_CMD_BYTES:
        params = "&".join(_argv_params(command))
        return f"{base}&{params}&service={service_name}"
    # Oversized: an interactive shell with no command. The caller delivers it over STDIN
    # (code 104). Unchanged -- this path exists for URL length, not for quoting.
    return f"{base}&cmd0=%2Fbin%2Fsh&service={service_name}"


# ---------------------------------------------------------------------------
# Frame tracing — the instrument that separates drop / reorder / synthetic zero
# ---------------------------------------------------------------------------
TRACE_ENV = "AKASH_LEASE_TRACE_FRAMES"


class FrameTrace:
    """Opt-in per-frame record: ``(code, payload_len, rel_time)``.

    ★ WHY THIS EXISTS. ``rc==0`` with empty stdout has at least three candidate
    mechanisms and they are indistinguishable from the outcome alone:

      * a genuine upstream **DROP** — no ``stdout(100)`` frame ever arrives
      * a **reorder** — stdout arrives AFTER ``result(102)`` and a drain recovers it
      * a **synthetic zero** — a lone 16-byte ``{"exit_code": 0}`` result frame and
        nothing else, which a closed/dead lease returns

    ⇒ Only the frame sequence tells them apart, and nothing in this repo recorded it.
    An entire night was spent inferring mechanism from CI-log outcomes because the
    direct instrument did not exist. Ported from just-akash's ``JUST_AKASH_TRACE_FRAMES``
    (``docs/exec-reliability-investigation.md``), which used it to refute the reorder
    hypothesis with 240/240 clean control execs.

    ⚠ THE INSTRUMENT MUST NOT PERTURB WHAT IT MEASURES. The residual drop is an upstream
    teardown race, so:

      * the list is allocated **only** when tracing is enabled — otherwise ``record`` is
        a single ``is None`` check
      * only plain appends happen in the receive loop; **no formatting, no I/O**
      * the human-readable line is rendered **after** the loop returns
      * ⛔ payload bytes are **never** recorded — only the code and the length

    ⚠ ONE monotonic read per frame, reused for both the tuple and ``t_result``, so a
    frame's two timestamps cannot disagree.
    """

    __slots__ = ("_frames", "_t0", "t_result", "close_reason", "close_at")

    def __init__(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = os.environ.get(TRACE_ENV) not in (None, "", "0")
        self._frames: list[tuple[int, int, float]] | None = [] if enabled else None
        self._t0 = time.monotonic()
        self.t_result: float | None = None
        self.close_reason: str | None = None
        self.close_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self._frames is not None

    def record(self, frame: bytes) -> None:
        """Append ``(code, payload_len, rel_time)``. A no-op when disabled."""
        if self._frames is None or not frame:
            return
        rel = round(time.monotonic() - self._t0, 4)
        self._frames.append((frame[0], len(frame) - 1, rel))
        if frame[0] == RESULT and self.t_result is None:
            self.t_result = rel

    def record_parts(self, code: int, payload_len: int) -> None:
        """Record an already-decoded frame without rebuilding its bytes.

        ⚠ A caller that has ``(code, payload)`` in hand would otherwise have to do
        ``record(bytes([code]) + payload)`` — an allocation and a copy of the whole
        payload, in the receive loop, on every frame. The instrument must not perturb
        the teardown race it measures, so it must not allocate proportionally to the
        data it is watching.
        """
        if self._frames is None:
            return
        rel = round(time.monotonic() - self._t0, 4)
        self._frames.append((code, payload_len, rel))
        if code == RESULT and self.t_result is None:
            self.t_result = rel

    @property
    def frames(self) -> list[tuple[int, int, float]]:
        return list(self._frames or ())

    def shape(self) -> str:
        """Ordered frame names, e.g. ``stdout,result``. ``""`` when nothing arrived."""
        names = {STDOUT: "stdout", STDERR: "stderr", RESULT: "result", FAILURE: "failure"}
        return ",".join(names.get(c, str(c)) for c, _, _ in (self._frames or ()))

    def stdout_bytes(self) -> int:
        return sum(ln for c, ln, _ in (self._frames or ()) if c == STDOUT)

    def classify(self) -> str:
        """The mechanism the frame sequence is consistent with.

        ⚠ CONSISTENT WITH, NOT PROOF OF. This reads the shape only; a closed lease and a
        provider that never wrote to stdout produce the same sequence. Pair it with an
        out-of-band liveness check before attributing a cause — that check is what
        falsified the reference investigation's own strongest mid-course claim, and its
        absence is what let a symptom match survive three times in one night here.
        """
        if self._frames is None:
            return "not_traced"
        if not self._frames:
            return "no_frames"
        codes = [c for c, _, _ in self._frames]
        if STDOUT in codes:
            first_out = codes.index(STDOUT)
            if RESULT in codes and codes.index(RESULT) < first_out:
                return "reorder"
            return "stdout_present"
        if codes == [RESULT] and self._frames[0][1] <= 32:
            return "lone_small_result"
        if RESULT in codes:
            return "no_stdout_frame"
        return "no_result_frame"

    def close(self, reason: str) -> None:
        """Record WHY the receive loop ended, and when.

        ★ THE DISCRIMINATOR ``classify()`` CANNOT PROVIDE. Two very different failures
        both yield an empty frame list:

          * the peer closed the connection before sending anything
          * the loop ended without ever receiving a result frame

        Same shape, different layer — and an ``exit=-1`` with no frames is exactly where
        that distinction decides the diagnosis. The frame sequence answers "what arrived";
        only the close answers "why it stopped".

        ⚠ ``rel_time`` here is measured from the same ``t0`` as the frames, so the gap
        between the last frame and the close is readable directly: a close 0.5s into a
        30s budget with no frames is a connection-layer event, while a close after a long
        silence is a timeout.
        """
        if self._frames is None:
            return
        self.close_reason = reason
        self.close_at = round(time.monotonic() - self._t0, 4)

    def render(self, recovered: int = 0) -> str:
        """The one-line summary. Called AFTER the receive loop, never inside it."""
        tr = f"{self.t_result:.3f}s" if self.t_result is not None else "none"
        at = self.close_at if self.close_at is not None else "n/a"
        return (
            f"[lease-shell] FRAME-TRACE shape=[{self.shape()}] "
            f"stdout_bytes={self.stdout_bytes()} recovered={recovered} "
            f"t_result={tr} classify={self.classify()} "
            f"close={self.close_reason or 'n/a'}@{at} "
            f"frames={self.frames}"
        )


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
