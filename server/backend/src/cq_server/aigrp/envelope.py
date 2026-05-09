r"""AIGRP message envelope: HMAC-SHA256 sign + verify with replay protection.

Decision 28 §1.6 — AIGRP authentication is integrity + authenticity, not
confidentiality (TLS already provides confidentiality between L2 ALBs).
HMAC-SHA256 over a canonicalized envelope. AEAD was rejected as adding
nonce/replay complexity without a threat-model gain.

Wire shape::

    {
      "version": "v1",
      "pair_id": "<lex-min canonical pair name>",
      "src_l2_id": "...",
      "dst_l2_id": "...",
      "ts": "RFC3339-UTC",
      "nonce": "<16 random bytes b64u>",
      "msg_id": "<UUIDv4>",
      "payload": <canonical JSON of message body>
    }
    mac = HMAC-SHA256(pair_secret, JCS-canonical-bytes(envelope))
    on-wire = envelope_json + "\n" + base64url(mac)

Replay protection:
  - sliding-window cache of ``(src_l2_id, msg_id)`` for 10 min;
  - reject ``ts`` outside ±5 min;
  - reject duplicate ``msg_id``.

Direction asymmetry is encoded in ``src_l2_id`` / ``dst_l2_id`` so that
even though one ``pair_id`` covers both directions, cross-direction
replay (Bob's signed message replayed as if from Alice) is rejected.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from ..crypto import b64u, b64u_decode, canonicalize
from .pair_secret import lex_min_canonical

ENVELOPE_VERSION = "v1"
_NONCE_BYTES = 16
_REPLAY_TS_TOLERANCE_SEC = 300  # ±5 min
_REPLAY_CACHE_TTL_SEC = 600  # 10 min sliding window
_REPLAY_CACHE_MAX = 100_000  # bound memory; eviction at 2× capacity


class EnvelopeVerificationError(Exception):
    """Raised when verify_envelope rejects an envelope.

    Wraps four distinct failure modes (bad mac, ts out of bounds,
    replayed msg_id, malformed envelope). Catch this when you want a
    single broad reject; check ``reason`` for telemetry.
    """

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        """Construct with a coarse ``reason`` tag + optional human ``detail``."""
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _canonical_pair_id(src_l2_id: str, dst_l2_id: str) -> str:
    """Lex-min canonical pair-id string.

    Same shape as ``pair_secret.lex_min_canonical`` but returned as str
    (envelope JSON, not raw HMAC info bytes).
    """
    return lex_min_canonical(src_l2_id, dst_l2_id).decode("utf-8")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sign_envelope(
    *,
    pair_secret: bytes,
    src_l2_id: str,
    dst_l2_id: str,
    payload: dict[str, Any],
    ts: str | None = None,
    nonce: bytes | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Build a signed AIGRP envelope.

    Args:
        pair_secret: 32-byte HMAC key (from ``derive_pair_secret``).
        src_l2_id: sender L2 id.
        dst_l2_id: intended receiver L2 id. Must differ from ``src_l2_id``.
        payload: arbitrary JSON-serialisable dict.
        ts: optional override (tests pin time); default ``now`` UTC.
        nonce: optional override (tests pin); default 16 random bytes.
        msg_id: optional override (tests pin); default UUIDv4.

    Returns:
        dict with envelope fields + ``mac`` (b64u). Caller serialises
        this for the wire; we don't impose a transport.
    """
    if len(pair_secret) != 32:
        raise ValueError(f"pair_secret must be 32 bytes, got {len(pair_secret)}")
    envelope = {
        "version": ENVELOPE_VERSION,
        "pair_id": _canonical_pair_id(src_l2_id, dst_l2_id),
        "src_l2_id": src_l2_id,
        "dst_l2_id": dst_l2_id,
        "ts": ts or _now_iso(),
        "nonce": b64u(nonce or secrets.token_bytes(_NONCE_BYTES)),
        "msg_id": msg_id or str(uuid.uuid4()),
        "payload": payload,
    }
    mac = hmac.new(pair_secret, canonicalize(envelope), "sha256").digest()
    out = dict(envelope)
    out["mac"] = b64u(mac)
    return out


# ---------------------------------------------------------------------------
# Replay cache — a small bounded LRU keyed by ``(src_l2_id, msg_id)``.
# ---------------------------------------------------------------------------


class _ReplayCache:
    """Sliding-window dedup cache, threadsafe.

    Lookup is O(1); pruning is amortised O(1) by ordering on insertion.
    On insert, we walk from the LRU end discarding entries older than
    the TTL — bounded by the number-of-evictions, not cache size.
    """

    def __init__(self, *, ttl_sec: int = _REPLAY_CACHE_TTL_SEC, max_size: int = _REPLAY_CACHE_MAX) -> None:
        self._ttl = ttl_sec
        self._max = max_size
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_record(self, src_l2_id: str, msg_id: str, *, now: float | None = None) -> bool:
        """Return True if (src, msg_id) is fresh; False if already seen.

        On True, the entry is recorded so a subsequent call returns False.
        """
        key = (src_l2_id, msg_id)
        ts = now if now is not None else time.monotonic()
        with self._lock:
            self._evict_expired(ts)
            if key in self._entries:
                return False
            self._entries[key] = ts
            if len(self._entries) > self._max:
                # Hard cap: drop oldest. Operationally this means a flood
                # of distinct msg_ids could cause a real replay to slip
                # through after eviction, but the hard cap is a DoS guard.
                self._entries.popitem(last=False)
            return True

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._entries:
            k, t = next(iter(self._entries.items()))
            if t < cutoff:
                self._entries.popitem(last=False)
            else:
                break

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_default_replay_cache = _ReplayCache()


def reset_replay_cache_for_tests() -> None:
    """Clear the module-level replay cache. Tests only."""
    _default_replay_cache.clear()


def _parse_iso(ts: str) -> datetime:
    """Parse RFC3339/ISO-8601, accepting trailing Z."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def verify_envelope(
    *,
    envelope: dict[str, Any],
    pair_secret: bytes,
    expected_dst_l2_id: str,
    replay_cache: _ReplayCache | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a signed AIGRP envelope; return the payload on success.

    Raises ``EnvelopeVerificationError`` on any failure. The exception's
    ``reason`` field is one of:
    ``malformed`` | ``version`` | ``pair_mismatch`` | ``dst_mismatch`` |
    ``ts_out_of_bounds`` | ``replay`` | ``mac``.
    """
    if not isinstance(envelope, dict):
        raise EnvelopeVerificationError("malformed", detail="envelope is not a dict")
    required = ("version", "pair_id", "src_l2_id", "dst_l2_id", "ts", "nonce", "msg_id", "payload", "mac")
    for k in required:
        if k not in envelope:
            raise EnvelopeVerificationError("malformed", detail=f"missing field {k!r}")
    if envelope["version"] != ENVELOPE_VERSION:
        raise EnvelopeVerificationError("version", detail=f"got {envelope['version']!r}")

    src = envelope["src_l2_id"]
    dst = envelope["dst_l2_id"]
    if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
        raise EnvelopeVerificationError("malformed", detail="src/dst must be non-empty strings")
    if dst != expected_dst_l2_id:
        # Encodes "this envelope was sent to someone else" — also blocks
        # cross-direction replay (Alice→Bob can't be replayed as
        # Bob→Alice because dst differs).
        raise EnvelopeVerificationError("dst_mismatch", detail=f"expected {expected_dst_l2_id!r}, got {dst!r}")

    expected_pair = _canonical_pair_id(src, dst)
    if envelope["pair_id"] != expected_pair:
        raise EnvelopeVerificationError(
            "pair_mismatch", detail=f"expected {expected_pair!r}, got {envelope['pair_id']!r}"
        )

    if not isinstance(envelope["msg_id"], str) or not envelope["msg_id"]:
        raise EnvelopeVerificationError("malformed", detail="msg_id must be non-empty string")
    try:
        b64u_decode(envelope["nonce"])
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeVerificationError("malformed", detail=f"nonce not b64url: {exc!r}") from exc

    # Timestamp window
    try:
        ts_dt = _parse_iso(envelope["ts"])
    except (TypeError, ValueError) as exc:
        raise EnvelopeVerificationError("malformed", detail=f"ts not ISO-8601: {exc!r}") from exc
    if ts_dt.tzinfo is None:
        raise EnvelopeVerificationError("malformed", detail="ts must be timezone-aware (UTC offset required)")
    now_dt = now or datetime.now(UTC)
    skew = abs((now_dt - ts_dt).total_seconds())
    if skew > _REPLAY_TS_TOLERANCE_SEC:
        raise EnvelopeVerificationError(
            "ts_out_of_bounds",
            detail=f"|now - ts| = {skew:.0f}s > {_REPLAY_TS_TOLERANCE_SEC}s",
        )

    # MAC verify (constant-time)
    if not isinstance(envelope["mac"], str):
        raise EnvelopeVerificationError("malformed", detail="mac must be string")
    received_mac_b64 = envelope["mac"]
    try:
        received_mac = b64u_decode(received_mac_b64)
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeVerificationError("malformed", detail=f"mac not b64url: {exc!r}") from exc
    envelope_for_mac = {k: v for k, v in envelope.items() if k != "mac"}
    expected_mac = hmac.new(pair_secret, canonicalize(envelope_for_mac), "sha256").digest()
    if not hmac.compare_digest(expected_mac, received_mac):
        raise EnvelopeVerificationError("mac", detail="HMAC mismatch")

    # Replay check is the LAST step so an invalid envelope doesn't
    # poison the cache with a junk msg_id.
    cache = replay_cache or _default_replay_cache
    if not cache.check_and_record(src, envelope["msg_id"]):
        raise EnvelopeVerificationError("replay", detail=f"msg_id {envelope['msg_id']!r} from {src!r} already seen")

    return envelope["payload"]


# Test helper: build a fresh replay cache to inject into verify_envelope.
def make_replay_cache(*, ttl_sec: int = _REPLAY_CACHE_TTL_SEC, max_size: int = _REPLAY_CACHE_MAX) -> _ReplayCache:
    """Construct a standalone replay cache (tests use this)."""
    return _ReplayCache(ttl_sec=ttl_sec, max_size=max_size)


def replay_window_seconds() -> tuple[int, int]:
    """Return (ts_tolerance_sec, replay_cache_ttl_sec) — for docs/tests."""
    return _REPLAY_TS_TOLERANCE_SEC, _REPLAY_CACHE_TTL_SEC


__all__ = [
    "ENVELOPE_VERSION",
    "EnvelopeVerificationError",
    "make_replay_cache",
    "replay_window_seconds",
    "reset_replay_cache_for_tests",
    "sign_envelope",
    "verify_envelope",
]
