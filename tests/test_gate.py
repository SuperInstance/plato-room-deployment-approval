"""
Tests for the Deployment Approval Room's gate logic.

Run: python -m pytest tests/test_gate.py -v
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plato_room_deployment_approval.gate import (
    run_all_gates,
    deployment_decision,
    check_ci_passing,
    check_coverage_delta,
    check_security_scan,
    check_diff_size,
    check_force_push_main,
    check_rate_limit,
    check_review_approval,
    GateContext,
    GateResult,
    ConservationTracker,
)


# ─── Fixtures ────────────────────────────────────────────────

def make_ctx(**kwargs) -> GateContext:
    """Create a GateContext with defaults."""
    defaults = dict(
        pr_info={
            "base_ref": "main",
            "additions": 100,
            "deletions": 20,
            "force_push": False,
        },
        ci_status={"passed": 4, "failed": 0, "pending": 0, "total": 4},
        reviews={"approvals": 2, "changes_requested": 0},
        coverage_base=80.0,
        coverage_head=82.0,
        security_findings={"critical": 0, "error": 0},
        deployments_today=3,
        max_daily_deployments=10,
    )
    defaults.update(kwargs)
    return GateContext(**defaults)


# ─── Conservation Tracker tests ──────────────────────────────

class TestConservationTracker:
    def test_initial_state(self):
        tracker = ConservationTracker(max_per_day=5)
        assert tracker.count_today() == 0
        assert tracker.remaining() == 5
        assert tracker.can_deploy() is True

    def test_record_increments_count(self):
        tracker = ConservationTracker(max_per_day=5)
        tracker.record()
        tracker.record()
        assert tracker.count_today() == 2
        assert tracker.remaining() == 3

    def test_cannot_exceed_limit(self):
        tracker = ConservationTracker(max_per_day=2)
        tracker.record()
        tracker.record()
        assert tracker.can_deploy() is False
        assert tracker.remaining() == 0

    def test_prunes_old_deployments(self):
        tracker = ConservationTracker(max_per_day=3)
        old = time.time() - 100000  # > 24h ago
        tracker.record(old)
        tracker.record(old)
        tracker.record(time.time())
        assert tracker.count_today() == 1

    def test_serialization(self):
        tracker = ConservationTracker(max_per_day=7)
        tracker.record()
        tracker.record()
        data = tracker.to_dict()
        restored = ConservationTracker.from_dict(data)
        assert restored.max_per_day == 7
        assert restored.count_today() == 2


# ─── Gate tests ──────────────────────────────────────────────

class TestCIPassing:
    def test_all_passing(self):
        ctx = make_ctx()
        result = check_ci_passing(ctx)
        assert result.passed

    def test_failing_ci_blocks(self):
        ctx = make_ctx(ci_status={"passed": 3, "failed": 1, "pending": 0, "total": 4})
        result = check_ci_passing(ctx)
        assert not result.passed
        assert result.blocking
        assert result.severity == "critical"

    def test_pending_ci_blocks(self):
        ctx = make_ctx(ci_status={"passed": 3, "failed": 0, "pending": 1, "total": 4})
        result = check_ci_passing(ctx)
        assert not result.passed
        assert result.blocking


class TestCoverageDelta:
    def test_improving_coverage(self):
        ctx = make_ctx(coverage_base=80.0, coverage_head=85.0)
        result = check_coverage_delta(ctx)
        assert result.passed

    def test_small_drop_ok(self):
        ctx = make_ctx(coverage_base=80.0, coverage_head=77.0, max_coverage_drop_pct=5.0)
        result = check_coverage_delta(ctx)
        assert result.passed  # 3% drop < 5% threshold

    def test_large_drop_blocks(self):
        ctx = make_ctx(coverage_base=80.0, coverage_head=70.0, max_coverage_drop_pct=5.0)
        result = check_coverage_delta(ctx)
        assert not result.passed
        assert result.blocking
        assert result.severity == "error"

    def test_no_coverage_data(self):
        ctx = make_ctx(coverage_base=0.0, coverage_head=0.0)
        result = check_coverage_delta(ctx)
        assert result.passed


class TestSecurityScan:
    def test_no_findings(self):
        ctx = make_ctx()
        result = check_security_scan(ctx)
        assert result.passed

    def test_critical_blocks(self):
        ctx = make_ctx(security_findings={"critical": 1, "error": 0})
        result = check_security_scan(ctx)
        assert not result.passed
        assert result.blocking
        assert result.severity == "critical"

    def test_error_blocks(self):
        ctx = make_ctx(security_findings={"critical": 0, "error": 2})
        result = check_security_scan(ctx)
        assert not result.passed
        assert result.blocking


class TestDiffSize:
    def test_small_diff_ok(self):
        ctx = make_ctx(pr_info={"additions": 100, "deletions": 20, "base_ref": "main"})
        result = check_diff_size(ctx)
        assert result.passed

    def test_large_diff_warns(self):
        ctx = make_ctx(
            pr_info={"additions": 800, "deletions": 300, "base_ref": "main"},
            diff_warn_lines=500,
        )
        result = check_diff_size(ctx)
        assert not result.passed
        assert not result.blocking  # Warning only
        assert result.severity == "warning"

    def test_huge_diff_blocks(self):
        ctx = make_ctx(
            pr_info={"additions": 4000, "deletions": 2000, "base_ref": "main"},
            diff_block_lines=5000,
        )
        result = check_diff_size(ctx)
        assert not result.passed
        assert result.blocking
        assert result.severity == "error"


class TestForcePushMain:
    def test_no_force_push(self):
        ctx = make_ctx(pr_info={"base_ref": "main", "force_push": False})
        result = check_force_push_main(ctx)
        assert result.passed

    def test_force_push_main_blocks(self):
        ctx = make_ctx(pr_info={"base_ref": "main", "force_push": True})
        result = check_force_push_main(ctx)
        assert not result.passed
        assert result.blocking
        assert result.severity == "critical"

    def test_force_push_feature_branch_ok(self):
        ctx = make_ctx(pr_info={"base_ref": "develop", "force_push": True})
        result = check_force_push_main(ctx)
        assert result.passed


class TestRateLimit:
    def test_under_limit(self):
        ctx = make_ctx(deployments_today=3, max_daily_deployments=10)
        result = check_rate_limit(ctx)
        assert result.passed

    def test_at_limit_blocks(self):
        ctx = make_ctx(deployments_today=10, max_daily_deployments=10)
        result = check_rate_limit(ctx)
        assert not result.passed
        assert result.blocking
        assert result.severity == "critical"
        assert "Conservation law" in result.message

    def test_over_limit_blocks(self):
        ctx = make_ctx(deployments_today=15, max_daily_deployments=10)
        result = check_rate_limit(ctx)
        assert not result.passed
        assert result.blocking


class TestReviewApproval:
    def test_sufficient_approvals(self):
        ctx = make_ctx(reviews={"approvals": 2, "changes_requested": 0})
        result = check_review_approval(ctx)
        assert result.passed

    def test_changes_requested_blocks(self):
        ctx = make_ctx(reviews={"approvals": 2, "changes_requested": 1})
        result = check_review_approval(ctx)
        assert not result.passed
        assert result.blocking

    def test_insufficient_approvals_blocks(self):
        ctx = make_ctx(
            reviews={"approvals": 0, "changes_requested": 0},
            required_reviewers=1,
        )
        result = check_review_approval(ctx)
        assert not result.passed
        assert result.blocking


# ─── Orchestration tests ─────────────────────────────────────

class TestRunAllGates:
    def test_returns_all_gates(self):
        ctx = make_ctx()
        results = run_all_gates(ctx)
        gate_ids = {r.gate_id for r in results}
        assert "ci_passing" in gate_ids
        assert "coverage_delta" in gate_ids
        assert "security_scan" in gate_ids
        assert "diff_size" in gate_ids
        assert "force_push_main" in gate_ids
        assert "rate_limit" in gate_ids
        assert "review_approval" in gate_ids

    def test_all_passing_with_good_ctx(self):
        ctx = make_ctx()
        results = run_all_gates(ctx)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 0, f"Unexpected failures: {[r.name for r in failed]}"

    def test_empty_ctx_doesnt_crash(self):
        ctx = GateContext()
        results = run_all_gates(ctx)
        assert len(results) > 0


class TestDeploymentDecision:
    def test_all_passing_approves(self):
        ctx = make_ctx()
        results = run_all_gates(ctx)
        decision, blocking = deployment_decision(results)
        assert decision == "APPROVED"
        assert len(blocking) == 0

    def test_failing_ci_blocks(self):
        ctx = make_ctx(ci_status={"passed": 0, "failed": 2, "pending": 0, "total": 2})
        results = run_all_gates(ctx)
        decision, blocking = deployment_decision(results)
        assert decision == "BLOCKED"
        assert len(blocking) >= 1

    def test_force_push_blocks(self):
        ctx = make_ctx(pr_info={"base_ref": "main", "force_push": True, "additions": 10, "deletions": 5})
        results = run_all_gates(ctx)
        decision, blocking = deployment_decision(results)
        assert decision == "BLOCKED"

    def test_rate_limit_blocks(self):
        ctx = make_ctx(deployments_today=10, max_daily_deployments=10)
        results = run_all_gates(ctx)
        decision, blocking = deployment_decision(results)
        assert decision == "BLOCKED"

    def test_warning_only_warns(self):
        ctx = make_ctx(
            pr_info={"additions": 800, "deletions": 300, "base_ref": "main", "force_push": False},
            diff_warn_lines=500,
            diff_block_lines=5000,
        )
        results = run_all_gates(ctx)
        decision, blocking = deployment_decision(results)
        assert decision == "WARNED"
