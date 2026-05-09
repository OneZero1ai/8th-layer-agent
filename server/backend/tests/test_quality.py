"""Tests for the propose-time content quality guards."""

from __future__ import annotations

from cq.models import Insight

from cq_server.quality import check_propose_quality


def _good_insight() -> Insight:
    return Insight(
        summary="Use connection pooling for DB clients",
        detail="Database connections are expensive to create at request time.",
        action="Configure a connection pool with a max size of 10.",
    )


def _good_domains() -> list[str]:
    return ["databases", "performance"]


class TestPlaceholderDomains:
    def test_test_only_domain_rejected(self) -> None:
        reason = check_propose_quality(["test"], _good_insight())
        assert reason is not None
        assert "placeholder" in reason

    def test_foo_only_domain_rejected(self) -> None:
        reason = check_propose_quality(["foo"], _good_insight())
        assert reason is not None

    def test_example_only_domain_rejected(self) -> None:
        reason = check_propose_quality(["example"], _good_insight())
        assert reason is not None

    def test_test_with_other_domain_accepted(self) -> None:
        # If the agent mixes 'test' with a real tag (e.g. 'pytest', 'testing'),
        # we accept — the placeholder check is on the entire set being trivial.
        reason = check_propose_quality(["test", "pytest"], _good_insight())
        assert reason is None


class TestPlaceholderSummary:
    def test_summary_test_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "test"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None

    def test_summary_test_with_punctuation_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "Test."
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None

    def test_summary_lorem_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "lorem"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None

    def test_summary_todo_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "todo"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None


class TestLengthGuards:
    def test_too_short_summary_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "ok"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None
        assert "summary" in reason.lower()

    def test_too_short_detail_rejected(self) -> None:
        ins = _good_insight()
        ins.detail = "yes"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None
        assert "detail" in reason.lower()

    def test_too_short_action_rejected(self) -> None:
        ins = _good_insight()
        ins.action = "fix"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None
        assert "action" in reason.lower()


class TestSummaryEqualsDetail:
    def test_identical_strings_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "Use connection pooling for the database client"
        ins.detail = "Use connection pooling for the database client"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None
        assert "identical" in reason.lower()

    def test_case_only_difference_rejected(self) -> None:
        ins = _good_insight()
        ins.summary = "Use Connection Pooling for the database client"
        ins.detail = "use connection pooling for the database client"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is not None


class TestAcceptanceCases:
    def test_well_formed_insight_accepted(self) -> None:
        reason = check_propose_quality(_good_domains(), _good_insight())
        assert reason is None

    def test_long_summary_accepted(self) -> None:
        ins = _good_insight()
        ins.summary = "A very specific gotcha that took an hour to debug and is worth sharing"
        reason = check_propose_quality(_good_domains(), ins)
        assert reason is None

    def test_real_world_propose_shape_accepted(self) -> None:
        # Mirror the shape Strands probes produce in production.
        reason = check_propose_quality(
            ["lambda", "vpc", "cold-start"],
            Insight(
                summary="VPC-attached Lambda functions cannot reach public APIs by default",
                # Long fixture strings mirror real Strands probe payloads — wrapping would distort the test data.
                detail="When a Lambda is configured with a VPC, outbound traffic routes through the VPC. Without a NAT gateway or VPC endpoint, public APIs are unreachable. The error appears as a generic timeout, not a network-error, masking the cause.",  # noqa: E501
                action="Add a NAT gateway in a public subnet and route the Lambda's private subnet through it, OR add VPC endpoints for the specific AWS services the Lambda calls.",  # noqa: E501
            ),
        )
        assert reason is None
