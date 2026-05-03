"""Reputation chain + root verifier (task #108 sub-task 7).

A small library callable from any context: tests, the directory's
``/reputation/root`` validator, peer-Enterprise audit pipelines.

Three verification levels, callable independently:

- ``verify_chain(events)`` — checks that each event's ``prev_event_hash``
  matches the preceding event's ``payload_hash``. Catches reordering,
  insertion, deletion, and mutation of any event in the middle of the
  chain. Doesn't need signatures — works on v1-alpha unsigned chains.

- ``verify_event_signatures(events)`` — for each event with non-NULL
  ``signature_b64u``, verifies the signature against ``payload_canonical``
  using ``signing_key_id`` as the b64url public key. Skips unsigned
  events (returns ``ok=True`` with ``unsigned_count`` reflecting the skip).

- ``verify_root(root, events)`` — re-derives the Merkle root from the
  supplied event list (must match the (enterprise_id, root_date)
  window), compares to ``root.merkle_root_hash``, and verifies the
  root's own signature against its canonical envelope.

Each function returns a small dataclass-like dict with ``ok: bool`` plus
diagnostic counters; callers compose them as needed. No exceptions on
verification failure — failure is normal in audit code, not exceptional.
"""

from __future__ import annotations

from typing import Any

from .crypto import verify_raw
from .merkle import merkle_root
from .reputation import canonical_payload_bytes, compute_payload_hash


def verify_chain(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify ``prev_event_hash`` linkage across an ordered event list.

    Args:
        events: list of event rows (dicts with at least
            ``payload_hash`` and ``prev_event_hash``), sorted by
            ``ts`` ASC. The caller is responsible for the ordering;
            this function just walks the array.

    Returns:
        ``{ok, broken_at_index, broken_event_id, count}``. ``ok=False``
        when any event N>0 has ``prev_event_hash != events[N-1].payload_hash``.
    """
    if not events:
        return {"ok": True, "broken_at_index": None, "broken_event_id": None, "count": 0}

    for i in range(1, len(events)):
        prev_hash = events[i].get("prev_event_hash")
        expected = events[i - 1].get("payload_hash")
        if prev_hash != expected:
            return {
                "ok": False,
                "broken_at_index": i,
                "broken_event_id": events[i].get("event_id"),
                "count": len(events),
            }
    return {"ok": True, "broken_at_index": None, "broken_event_id": None, "count": len(events)}


def verify_event_payload_hashes(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-derive ``payload_hash`` from ``payload_canonical`` and compare.

    Catches a row whose ``payload_canonical`` was mutated post-write
    (e.g. someone edited the JSON in-place). The chain check above
    catches reordering / linkage breaks; this one catches in-place
    payload tampering.
    """
    bad: list[str] = []
    for ev in events:
        canonical = ev.get("payload_canonical", "").encode("utf-8")
        if compute_payload_hash(canonical) != ev.get("payload_hash"):
            bad.append(ev.get("event_id", "<no-id>"))
    return {
        "ok": not bad,
        "tampered_event_ids": bad,
        "count": len(events),
    }


def verify_event_signatures(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify Ed25519 signatures on every signed event in the list.

    Unsigned events (signature_b64u IS NULL) are counted but not
    treated as failures — v1-alpha rows are unsigned by design and
    callers may want to allow them during the rollout window. To
    require all events be signed, check the returned ``unsigned_count``
    is zero in addition to ``ok``.
    """
    bad: list[str] = []
    unsigned = 0
    for ev in events:
        sig = ev.get("signature_b64u")
        kid = ev.get("signing_key_id")
        if not sig or not kid:
            unsigned += 1
            continue
        canonical = ev.get("payload_canonical", "").encode("utf-8")
        if not verify_raw(kid, canonical, sig):
            bad.append(ev.get("event_id", "<no-id>"))
    return {
        "ok": not bad,
        "bad_signature_event_ids": bad,
        "unsigned_count": unsigned,
        "signed_count": len(events) - unsigned,
        "count": len(events),
    }


def verify_root(
    root: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Verify a daily Merkle root against its event list.

    Two checks:
    1. Re-derive Merkle root from event payload_hashes (sorted by ts
       ASC; caller must pre-sort to match how the root was computed)
       and compare to ``root.merkle_root_hash``.
    2. If ``root.signature_b64u`` is non-NULL, verify against the
       canonical envelope shape used at compute time:
       ``{enterprise_id, root_date, event_count, merkle_root_hash,
          first_event_id, last_event_id}``.

    Args:
        root: a row from the ``reputation_roots`` table (dict with
            ``enterprise_id``, ``root_date``, ``event_count``,
            ``merkle_root_hash``, ``first_event_id``, ``last_event_id``,
            ``signature_b64u``, ``signing_key_id``).
        events: the events claimed to underlie this root, sorted ts ASC.
            Pass exactly the events for the (enterprise_id, root_date)
            window — this function does NOT re-filter.

    Returns:
        ``{ok, root_matches_events, signature_valid, count}``. ``ok``
        is the conjunction.
    """
    leaf_hashes = [e.get("payload_hash") for e in events]
    recomputed = merkle_root(leaf_hashes)
    root_matches = recomputed == root.get("merkle_root_hash")

    sig = root.get("signature_b64u")
    kid = root.get("signing_key_id")
    sig_valid: bool | None = None  # None means "no signature to check"
    if sig and kid:
        canonical = canonical_payload_bytes(
            {
                "enterprise_id": root.get("enterprise_id"),
                "root_date": root.get("root_date"),
                "event_count": root.get("event_count"),
                "merkle_root_hash": root.get("merkle_root_hash"),
                "first_event_id": root.get("first_event_id"),
                "last_event_id": root.get("last_event_id"),
            }
        )
        sig_valid = verify_raw(kid, canonical, sig)

    ok = root_matches and (sig_valid is not False)
    return {
        "ok": ok,
        "root_matches_events": root_matches,
        "signature_valid": sig_valid,
        "expected_root": recomputed,
        "actual_root": root.get("merkle_root_hash"),
        "count": len(events),
    }
