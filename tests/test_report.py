"""
Tests for the deployment approval report generator.

Run: python -m pytest tests/test_report.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plato_room_deployment_approval.gate import (
    GateResult, GateContext, run_all_gates, ConservationTracker,
)
from plato_room_deployment_approval.report import generate_report, generate_short_summary
from plato_room_deployment_approval.github_client import GitHubClient


# ─── Fixtures ────────────────────────────────────────────────

SAMPLE_RESULTS_ALL_PASS = [
    GateResult(gate_id="ci", name="CI Status", passed=True,
               message="All passing", severity="info"),
    GateResult(gate_id="coverage", name="Coverage", passed=True,
               message="85%", severity="info"),
]

SAMPLE_RESULTS_BLOCKED = [
    GateResult(gate_id="ci", name="CI Status", passed=False,
               message="2 CI checks failing", severity="critical", blocking=True),
    GateResult(gate_id="security", name="Security Scan", passed=False,
               message="1 critical finding", severity="critical", blocking=True),
    GateResult(gate_id="coverage", name="Coverage", passed=True,
               message="OK", severity="info"),
]

SAMPLE_RESULTS_WARNED = [
    GateResult(gate_id="diff", name="Diff Size", passed=False,
               message="Large diff: 1200 lines", severity="warning", blocking=False),
    GateResult(gate_id="ci", name="CI Status", passed=True,
               message="All passing", severity="info"),
]

SAMPLE_CONSERVATION = ConservationTracker(max_per_day=10)
SAMPLE_CONSERVATION.record()
SAMPLE_CONSERVATION.record()


# ─── Tests ───────────────────────────────────────────────────

class TestGenerateReport:
    def test_returns_string(self):
        report = generate_report(SAMPLE_RESULTS_ALL_PASS, decision="APPROVED")
        assert isinstance(report, str)

    def test_contains_header(self):
        report = generate_report(SAMPLE_RESULTS_ALL_PASS, decision="APPROVED")
        assert "PLATO Deployment Approval Report" in report

    def test_contains_decision(self):
        report = generate_report(SAMPLE_RESULTS_BLOCKED, decision="BLOCKED",
                                 blocking_failures=[r for r in SAMPLE_RESULTS_BLOCKED if not r.passed])
        assert "BLOCKED" in report

    def test_contains_summary(self):
        report = generate_report(SAMPLE_RESULTS_ALL_PASS, decision="APPROVED")
        assert "Summary" in report
        assert "Gates Run" in report

    def test_contains_gate_details(self):
        report = generate_report(SAMPLE_RESULTS_ALL_PASS, decision="APPROVED")
        assert "Gate Details" in report
        assert "CI Status" in report

    def test_contains_conservation_status(self):
        report = generate_report(
            SAMPLE_RESULTS_ALL_PASS, decision="APPROVED",
            conservation=SAMPLE_CONSERVATION,
        )
        assert "Conservation Status" in report
        assert "Daily Limit" in report

    def test_contains_repo_and_pr_info(self):
        report = generate_report(
            SAMPLE_RESULTS_ALL_PASS, decision="APPROVED",
            repo="owner/repo", pr_number=42, author="alice",
        )
        assert "owner/repo" in report
        assert "#42" in report
        assert "@alice" in report

    def test_lists_blocking_failures(self):
        blocking = [r for r in SAMPLE_RESULTS_BLOCKED if not r.passed]
        report = generate_report(SAMPLE_RESULTS_BLOCKED, decision="BLOCKED",
                                 blocking_failures=blocking)
        assert "CI Status" in report
        assert "Security Scan" in report

    def test_empty_results_dont_crash(self):
        report = generate_report([], decision="COMMENT")
        assert isinstance(report, str)

    def test_contains_footer(self):
        report = generate_report(SAMPLE_RESULTS_ALL_PASS, decision="APPROVED")
        assert "PLATO Deployment Approval Room" in report
        assert "github.com/SuperInstance" in report


class TestGenerateShortSummary:
    def test_all_passed(self):
        summary = generate_short_summary(SAMPLE_RESULTS_ALL_PASS, decision="APPROVED")
        assert "2/2" in summary
        assert "APPROVED" in summary

    def test_blocked(self):
        summary = generate_short_summary(SAMPLE_RESULTS_BLOCKED, decision="BLOCKED")
        assert "1/3" in summary
        assert "BLOCKED" in summary

    def test_warned(self):
        summary = generate_short_summary(SAMPLE_RESULTS_WARNED, decision="WARNED")
        assert "1/2" in summary
        assert "WARNED" in summary

    def test_empty(self):
        summary = generate_short_summary([], decision="COMMENT")
        assert "0/0" in summary
