"""Tests for the SHA-256 Merkle tree (task #108 sub-task 3)."""

from __future__ import annotations

import hashlib

from cq_server.merkle import EMPTY_DAY_ROOT, merkle_root


def _h(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


class TestEmptyAndSingle:
    def test_empty_returns_constant(self) -> None:
        assert merkle_root([]) == EMPTY_DAY_ROOT

    def test_empty_constant_is_stable(self) -> None:
        # Locking the constant down — any change is a wire-format change.
        expected = "sha256:" + hashlib.sha256(
            b"8l-reputation-empty-day-v1"
        ).hexdigest()
        assert EMPTY_DAY_ROOT == expected

    def test_single_leaf_root_is_just_that_leaf(self) -> None:
        leaf = _h("a")
        # With one leaf, the tree has no internal nodes — the leaf IS the root.
        assert merkle_root([leaf]) == leaf


class TestPairAndOdd:
    def test_two_leaves_pair_hash(self) -> None:
        a = _h("a")
        b = _h("b")
        # Manual computation: sha256(a_bytes || b_bytes)
        expected_root_bytes = hashlib.sha256(
            bytes.fromhex(a[len("sha256:"):]) + bytes.fromhex(b[len("sha256:"):])
        ).digest()
        assert merkle_root([a, b]) == "sha256:" + expected_root_bytes.hex()

    def test_three_leaves_duplicates_last(self) -> None:
        a, b, c = _h("a"), _h("b"), _h("c")
        # Tree shape with 3 leaves:
        #   root = H(H(a||b) || H(c||c))
        ab = hashlib.sha256(
            bytes.fromhex(a[7:]) + bytes.fromhex(b[7:])
        ).digest()
        cc = hashlib.sha256(
            bytes.fromhex(c[7:]) + bytes.fromhex(c[7:])
        ).digest()
        root = hashlib.sha256(ab + cc).digest()
        assert merkle_root([a, b, c]) == "sha256:" + root.hex()

    def test_order_is_load_bearing(self) -> None:
        a, b = _h("a"), _h("b")
        # Different order → different root.
        assert merkle_root([a, b]) != merkle_root([b, a])


class TestDeterminism:
    def test_same_input_same_root(self) -> None:
        leaves = [_h(s) for s in ("alpha", "beta", "gamma", "delta")]
        r1 = merkle_root(leaves)
        r2 = merkle_root(leaves)
        assert r1 == r2

    def test_root_matches_known_4_leaf_layout(self) -> None:
        a, b, c, d = _h("a"), _h("b"), _h("c"), _h("d")

        def cat(x: str, y: str) -> bytes:
            return bytes.fromhex(x[7:]) + bytes.fromhex(y[7:])

        ab = "sha256:" + hashlib.sha256(cat(a, b)).hexdigest()
        cd = "sha256:" + hashlib.sha256(cat(c, d)).hexdigest()
        expected_root = "sha256:" + hashlib.sha256(cat(ab, cd)).hexdigest()
        assert merkle_root([a, b, c, d]) == expected_root
