"""Propose-time content quality guards.

Reject KU shapes that are clearly placeholder, smoke-test, or internal-status
content before they pollute the commons. The /propose endpoint is the
choke-point — once a KU lands and is approved, removal requires admin DB
access (no public delete endpoint in cq's PoC).

These guards are intentionally narrow and conservative: only obvious junk
patterns. Real content-quality judgment lives client-side in /cq:reflect's
skill instructions; this is the server-side last-line-of-defense for
degenerate inputs that no honest agent should be sending.

Each guard returns a `(reject, reason)` tuple. None means accept; a string
means reject with that explanation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from cq.models import Insight

# Domain sets that indicate placeholder content. Comparison is order-insensitive.
PLACEHOLDER_DOMAIN_SETS: tuple[frozenset[str], ...] = (
    frozenset({"test"}),
    frozenset({"placeholder"}),
    frozenset({"foo"}),
    frozenset({"bar"}),
    frozenset({"example"}),
    frozenset({"sample"}),
)

# Summary text that's clearly a placeholder.
_PLACEHOLDER_SUMMARY_RE = re.compile(
    r"^(test|placeholder|sample|foo|bar|baz|example|lorem|todo)\.?\s*$",
    re.IGNORECASE,
)

# Hard minimum lengths. These bars are intentionally low — narrow enough to
# catch pure-junk inputs (summary='ok') without rejecting legitimately terse
# insights. The placeholder regex above handles the obvious one-word junk;
# these handle the general "too short to mean anything" case.
_MIN_SUMMARY_LEN = 20
_MIN_DETAIL_LEN = 30
_MIN_ACTION_LEN = 15


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _placeholder_domains(domains: Iterable[str]) -> bool:
    """True if the domain set is a known placeholder shape."""
    norm = frozenset(_normalize(d) for d in domains if d.strip())
    if not norm:
        return False
    return any(norm == placeholder for placeholder in PLACEHOLDER_DOMAIN_SETS)


def _placeholder_summary(summary: str) -> bool:
    return bool(_PLACEHOLDER_SUMMARY_RE.match((summary or "").strip()))


def _too_short(insight: Insight) -> str | None:
    if len(insight.summary.strip()) < _MIN_SUMMARY_LEN:
        return f"summary must be at least {_MIN_SUMMARY_LEN} chars"
    if len(insight.detail.strip()) < _MIN_DETAIL_LEN:
        return f"detail must be at least {_MIN_DETAIL_LEN} chars"
    if len(insight.action.strip()) < _MIN_ACTION_LEN:
        return f"action must be at least {_MIN_ACTION_LEN} chars"
    return None


def _summary_equals_detail(insight: Insight) -> bool:
    """Degenerate input where summary and detail say the same thing."""
    return _normalize(insight.summary) == _normalize(insight.detail)


def check_propose_quality(domains: list[str], insight: Insight) -> str | None:
    """Run all quality guards on a proposed KU.

    Returns None if the unit passes all guards; a human-readable rejection
    reason otherwise. Callers (e.g. the /propose endpoint) should map a
    non-None return to HTTP 422 with the reason as the detail.
    """
    if _placeholder_domains(domains):
        return "domain set is a placeholder shape (e.g. ['test']); use real domain tags"

    if _placeholder_summary(insight.summary):
        return "summary is a placeholder word; describe a specific insight"

    too_short = _too_short(insight)
    if too_short is not None:
        return too_short

    if _summary_equals_detail(insight):
        return "summary and detail are identical; detail must add context the summary doesn't"

    return None
