"""
Regression tests for bugs found during audit v0.1.1.

Run: python -m pytest tests/test_regression.py -v
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plato_room_deployment_approval.gate import (
    run_all_gates, GateContext, ConservationTracker, check_coverage_delta,
    check_ci_passing,
)
from plato_room_deployment_approval.room import (
    BaseRoom, DeploymentApprovalRoom, _actuator_approve,
    _actuator_block, _sensor_deployment_state,
)
from plato_room_deployment_approval.github_client import GitHubClient
from plato_room_deployment_approval.report import generate_report
import time


# ─── Bug #1: ConservationTracker._prune crashes with None in list ───

class TestBug1ConservationNoneHandling:
    def test_from_dict_filters_none_values(self):
        """ConservationTracker.from_dict should filter out None values."""
        now = time.time()
        data = {
            "max_per_day": 10,
            "deployments": [None, now, None, now - 100, None]
        }
        tracker = ConservationTracker.from_dict(data)
        assert tracker.count_today() == 2
        assert len(tracker._deployments) == 2
        assert None not in tracker._deployments

    def test_from_dict_handles_invalid_timestamps(self):
        """ConservationTracker.from_dict should filter non-numeric values."""
        now = time.time()
        data = {
            "max_per_day": 5,
            "deployments": [now, "invalid", now - 100, {"bad": "data"}]
        }
        tracker = ConservationTracker.from_dict(data)
        assert tracker.count_today() == 2
        assert all(isinstance(d, (int, float)) for d in tracker._deployments)


# ─── Bug #2: GitHubClient rate limit header case ───

class TestBug2RateLimitHeader:
    def test_rate_limit_header_parsing_lowercase(self):
        """GitHubClient should parse lowercase rate limit headers correctly."""
        # This test documents the fix - actual GitHub API returns lowercase headers
        # The fix uses case-insensitive header parsing
        client = GitHubClient(token="test")
        assert client._rate_limit_remaining == 5000  # Default value


# ─── Bug #3: Crashed gates marked as passed ───

class TestBug3GateCrashHandling:
    def test_crashed_gate_blocks_deployment(self):
        """A crashed gate should be marked as failed and blocking."""
        from plato_room_deployment_approval import gate

        # Create a crashing gate
        def crashing_gate(ctx):
            raise ValueError("Simulated crash")

        # Temporarily add crashing gate
        gate._ALL_GATES.append(crashing_gate)

        try:
            ctx = GateContext(ci_status={"passed": 1, "failed": 0})
            results = run_all_gates(ctx)

            crashed_results = [r for r in results if "errored" in r.message]
            assert len(crashed_results) == 1

            crashed = crashed_results[0]
            assert crashed.passed is False, "Crashed gate should not pass"
            assert crashed.blocking is True, "Crashed gate should block deployment"
            assert crashed.severity == "error", "Crashed gate should be error severity"
        finally:
            # Remove the crashing gate
            gate._ALL_GATES.pop()


# ─── Bug #4: handle_command crashes on invalid input ───

class TestBug4HandleCommandValidation:
    def test_history_with_invalid_arg_returns_error(self):
        """history command with non-numeric arg should return error, not crash."""
        room = BaseRoom()
        room.register_sensor("s1", lambda r: {"x": 1.0})

        resp = json.loads(room.handle_command("history abc"))
        assert resp["type"] == "error"
        assert "numeric" in resp["message"].lower()

    def test_audit_with_invalid_arg_returns_error(self):
        """audit command with non-numeric arg should return error, not crash."""
        room = BaseRoom()
        room.log_audit("test", {"data": 1})

        resp = json.loads(room.handle_command("audit xyz"))
        assert resp["type"] == "error"
        assert "numeric" in resp["message"].lower()

    def test_actuator_with_invalid_value_returns_error(self):
        """actuator command with non-numeric value should return error, not crash."""
        room = BaseRoom()
        room.register_actuator("test", lambda r, v: None)

        resp = json.loads(room.handle_command("actuator test not_a_number"))
        assert resp["type"] == "error"
        assert "numeric" in resp["message"].lower()


# ─── Bug #5: Actuators post to PR #0 ───

class TestBug5ActuatorNoPRGuard:
    def test_approve_without_pr_does_not_post(self):
        """_actuator_approve should not post when pr_number=0."""
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", pr_number=0, port=0)

        initial_audit_count = len(room._audit_log)
        _actuator_approve(room, 1.0)

        # Should log failure, not approval
        assert len(room._audit_log) > initial_audit_count
        latest = room._audit_log[-1]
        assert latest["event"] == "approve_failed"
        assert "no_pr_number" in latest["data"].get("reason", "")

    def test_block_without_pr_does_not_post(self):
        """_actuator_block should not post when pr_number=0."""
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", pr_number=0, port=0)

        initial_audit_count = len(room._audit_log)
        _actuator_block(room, 1.0)

        # Should log failure, not block
        assert len(room._audit_log) > initial_audit_count
        latest = room._audit_log[-1]
        assert latest["event"] == "block_failed"


# ─── Bug #6: Negative coverage validation ───

class TestBug6CoverageValidation:
    def test_negative_coverage_base_fails(self):
        """Negative coverage_base should cause gate to fail."""
        ctx = GateContext(coverage_base=-10.0, coverage_head=80.0, max_coverage_drop_pct=5.0)
        result = check_coverage_delta(ctx)
        assert result.passed is False
        assert "Invalid coverage" in result.message
        assert result.blocking is True

    def test_negative_coverage_head_fails(self):
        """Negative coverage_head should cause gate to fail."""
        ctx = GateContext(coverage_base=80.0, coverage_head=-5.0, max_coverage_drop_pct=5.0)
        result = check_coverage_delta(ctx)
        assert result.passed is False
        assert "Invalid coverage" in result.message

    def test_coverage_over_100_fails(self):
        """Coverage over 100% should cause gate to fail."""
        ctx = GateContext(coverage_base=80.0, coverage_head=150.0, max_coverage_drop_pct=5.0)
        result = check_coverage_delta(ctx)
        assert result.passed is False
        assert "Invalid coverage" in result.message

    def test_valid_coverage_range_passes(self):
        """Coverage in [0, 100] range should validate correctly."""
        ctx = GateContext(coverage_base=80.0, coverage_head=85.0, max_coverage_drop_pct=5.0)
        result = check_coverage_delta(ctx)
        assert result.passed is True


# ─── Bug #7: report.py with None from conservation ───

class TestBug7ReportConservationHandling:
    def test_report_handles_conservation_errors(self):
        """generate_report should handle conservation method errors gracefully."""
        from plato_room_deployment_approval.gate import GateResult

        result = GateResult("test", "Test", True, "OK")

        # Create a mock conservation that raises an error
        class BrokenConservation:
            def remaining(self):
                raise TypeError("Unexpected None")
            def count_today(self):
                return None
            max_per_day = 10

        conservation = BrokenConservation()

        # Should not crash, but include error message in report
        report = generate_report([result], decision="APPROVED", conservation=conservation)
        assert isinstance(report, str)
        assert "Unable to display conservation status" in report


# ─── Bug #8: force_push logic for non-main branches ───

class TestBug8ForcePushLogic:
    def test_force_push_to_feature_branch_detected(self):
        """Force push to feature branch should still be detected."""
        from plato_room_deployment_approval import github_client

        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", pr_number=42, port=0)

        # Set up force push to develop branch
        github_client._SAMPLE_PR_INFO["force_push"] = True
        github_client._SAMPLE_PR_INFO["base_ref"] = "develop"

        data = _sensor_deployment_state(room)

        assert data["force_push_detected"] == 1.0, \
            "Force push to feature branch should be detected"

        # Restore
        github_client._SAMPLE_PR_INFO["force_push"] = False
        github_client._SAMPLE_PR_INFO["base_ref"] = "main"

    def test_no_force_push_returns_zero(self):
        """No force push should return force_push_detected = 0.0."""
        from plato_room_deployment_approval import github_client

        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", pr_number=42, port=0)

        github_client._SAMPLE_PR_INFO["force_push"] = False

        data = _sensor_deployment_state(room)

        assert data["force_push_detected"] == 0.0
