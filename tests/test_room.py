"""
Tests for the GitHub client simulation mode and DeploymentApprovalRoom lifecycle.

Run: python -m pytest tests/test_room.py -v
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plato_room_deployment_approval.github_client import GitHubClient
from plato_room_deployment_approval.room import DeploymentApprovalRoom, BaseRoom, AlarmDef
from plato_room_deployment_approval.gate import (
    run_all_gates, deployment_decision, GateContext, ConservationTracker,
)


class TestGitHubClientSimulation:
    """GitHub client works without a token (simulation mode)."""

    def test_not_configured_without_token(self):
        client = GitHubClient(token="")
        assert not client.configured

    def test_fetch_pr_info_returns_sample(self):
        client = GitHubClient(token="")
        info = client.fetch_pr_info("owner", "repo", 1)
        assert info["state"] == "open"
        assert info["base_ref"] == "main"
        assert info["additions"] == 150

    def test_fetch_ci_status_returns_sample(self):
        client = GitHubClient(token="")
        status = client.fetch_ci_status("owner", "repo", "sha123")
        assert status["passed"] == 4
        assert status["failed"] == 0

    def test_fetch_reviews_returns_sample(self):
        client = GitHubClient(token="")
        reviews = client.fetch_reviews("owner", "repo", 1)
        assert reviews["approvals"] == 1
        assert reviews["changes_requested"] == 0

    def test_post_comment_simulated(self):
        client = GitHubClient(token="")
        result = client.post_comment("owner", "repo", 1, "Approved")
        assert result.get("simulated") is True

    def test_post_review_simulated(self):
        client = GitHubClient(token="")
        result = client.post_review("owner", "repo", 1, "LGTM", "APPROVE")
        assert result.get("simulated") is True

    def test_create_deployment_simulated(self):
        client = GitHubClient(token="")
        result = client.create_deployment("owner", "repo", "main")
        assert result.get("simulated") is True

    def test_apply_label_simulated(self):
        client = GitHubClient(token="")
        result = client.apply_label("owner", "repo", 1, "deployment-approved")
        assert result.get("simulated") is True


class TestRoomBasics:
    """Test the BaseRoom."""

    def test_base_room_registers_sensors(self):
        room = BaseRoom()
        room.register_sensor("test", lambda r: {"val": 1.0})
        assert "test" in room._sensors

    def test_base_room_registers_alarms(self):
        room = BaseRoom()
        room.register_alarm("overload", sensor="count", operator=">", threshold=10.0)
        assert "overload" in room._alarms

    def test_alarm_evaluation(self):
        alarm = AlarmDef("test", "val > 5", "val", ">", 5)
        assert alarm.evaluate({"val": 10.0}) is True
        assert alarm.evaluate({"val": 3.0}) is False
        assert alarm.evaluate({}) is False

    def test_alarm_operators(self):
        for op, val, threshold, expected in [
            ("<", 3, 5, True), ("<", 7, 5, False),
            (">", 7, 5, True), (">", 3, 5, False),
            ("<=", 5, 5, True), (">=", 5, 5, True),
            ("==", 5, 5, True), ("!=", 5, 5, False),
            (">=", 10, 10, True),
        ]:
            alarm = AlarmDef("t", f"v {op} {threshold}", "v", op, threshold)
            assert alarm.evaluate({"v": val}) is expected

    def test_tick_executes_sensors(self):
        room = BaseRoom()
        room.register_sensor("s1", lambda r: {"temp": 42.0})
        data = room.tick()
        assert data["temp"] == 42.0
        assert len(room._history) == 1

    def test_tick_evaluates_alarms(self):
        room = BaseRoom()
        room.register_sensor("s1", lambda r: {"temp": 100.0})
        room.register_alarm("hot", sensor="temp", operator=">", threshold=90.0)
        room.tick()
        assert room._alarms["hot"].state == "triggered"

    def test_tick_records_history(self):
        room = BaseRoom()
        room.register_sensor("s1", lambda r: {"x": float(r._seq)})
        for _ in range(5):
            room.tick()
        assert len(room._history) == 5
        assert room._history[-1]["seq"] == 5

    def test_audit_log(self):
        room = BaseRoom()
        room.log_audit("test_event", {"key": "value"})
        assert len(room._audit_log) == 1
        entry = room._audit_log[-1]
        assert entry["event"] == "test_event"
        assert entry["data"]["key"] == "value"

    def test_handle_tick_command(self):
        room = BaseRoom()
        room.register_sensor("s1", lambda r: {"x": 1.0})
        resp = json.loads(room.handle_command("tick"))
        assert resp["type"] == "tick"
        assert "x" in resp["data"]

    def test_handle_history_command(self):
        room = BaseRoom()
        room.register_sensor("s1", lambda r: {"x": 1.0})
        room.tick()
        room.tick()
        resp = json.loads(room.handle_command("history 2"))
        assert resp["type"] == "history"
        assert resp["count"] == 2

    def test_handle_audit_command(self):
        room = BaseRoom()
        room.log_audit("test", {"data": 1})
        resp = json.loads(room.handle_command("audit 1"))
        assert resp["type"] == "audit"
        assert resp["count"] == 1
        assert resp["entries"][0]["event"] == "test"

    def test_handle_alarm_list_command(self):
        room = BaseRoom()
        room.register_alarm("a1", sensor="s", operator=">", threshold=5)
        resp = json.loads(room.handle_command("alarm list"))
        assert resp["type"] == "alarm_list"
        assert resp["alarms"][0]["id"] == "a1"

    def test_handle_actuator_command(self):
        room = BaseRoom()
        called = []
        room.register_actuator("deploy", lambda r, v: called.append(v))
        resp = json.loads(room.handle_command("actuator deploy 1"))
        assert resp["type"] == "ack"
        assert called == [1.0]

    def test_handle_help_includes_audit(self):
        room = BaseRoom()
        resp = json.loads(room.handle_command("help"))
        assert "audit N" in resp["commands"]


class TestDeploymentApprovalRoom:
    """Test the DeploymentApprovalRoom specifically."""

    def test_setup_registers_all_sensors(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 1, port=0)
        assert "ci_status" in room._sensors
        assert "coverage_delta" in room._sensors
        assert "security_status" in room._sensors
        assert "deployment_state" in room._sensors
        assert "review_state" in room._sensors
        assert "diff_metrics" in room._sensors

    def test_setup_registers_all_actuators(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 1, port=0)
        assert "approve_deployment" in room._actuators
        assert "block_deployment" in room._actuators
        assert "post_approval" in room._actuators
        assert "trigger_rollback" in room._actuators

    def test_setup_registers_all_alarms(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 1, port=0)
        assert "coverage_drop" in room._alarms
        assert "ci_failing" in room._alarms
        assert "force_push_main" in room._alarms
        assert "rate_limit_exceeded" in room._alarms

    def test_room_id(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 1, port=0)
        assert room.room_id == "deployment-approval-room"

    def test_conservation_tracker_exists(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 1, port=0)
        assert room.conservation is not None
        assert room.conservation.max_per_day == 10

    def test_custom_conservation_limit(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 1, port=0,
                                       max_daily_deployments=5)
        assert room.conservation.max_per_day == 5

    def test_tick_with_sample_data(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 42, port=0)
        data = room.tick()
        # Sample data: CI passing, 1 approval
        assert data["ci_passed"] == 4
        assert data["ci_failed"] == 0
        assert data["review_approvals"] == 1
        assert data["diff_total"] > 0
        # No alarms should fire on good sample data
        assert room._alarms["ci_failing"].state == "idle"

    def test_tick_without_pr_number(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 0, port=0)
        data = room.tick()
        assert data["ci_passed"] == 0
        assert data["ci_failed"] == 0

    def test_history_records_all_ticks(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 42, port=0)
        for _ in range(3):
            room.tick()
        assert len(room._history) == 3

    def test_audit_log_on_actuate(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 42, port=0)
        room.actuate("block_deployment", 1.0)
        assert len(room._audit_log) >= 1
        assert room._audit_log[-1]["event"] == "blocked"

    def test_conservation_blocks_at_limit(self):
        gh = GitHubClient(token="")
        room = DeploymentApprovalRoom(gh, "owner", "repo", 42, port=0,
                                       max_daily_deployments=1)
        # First deployment succeeds
        room.actuate("approve_deployment", 1.0)
        assert room.conservation.count_today() == 1
        # Second deployment should be blocked by conservation law
        initial_audit_count = len(room._audit_log)
        room.actuate("approve_deployment", 1.0)
        # Should log a block, not another approval
        assert len(room._audit_log) > initial_audit_count
        latest = room._audit_log[-1]
        assert latest["event"] == "blocked"
