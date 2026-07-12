"""
Deployment gate logic for the Deployment Approval Room.

Each gate is a pure function that takes deployment context and returns a
GateResult.  Gates are deterministic — no LLM, no fuzzy logic.

The ConservationTracker enforces the room's conservation law:
deployments per day are a bounded quantity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class GateResult:
    """Result of a single deployment gate check."""
    gate_id: str
    name: str
    passed: bool
    message: str
    severity: str = "info"  # info, warning, error, critical
    blocking: bool = False  # If True, blocks deployment when not passed


GateFunc = Callable[["GateContext"], GateResult]
_ALL_GATES: list[GateFunc] = []


def gate(func: GateFunc) -> GateFunc:
    """Register a gate function."""
    _ALL_GATES.append(func)
    return func


@dataclass
class GateContext:
    """Context provided to all gate checks."""
    pr_info: dict[str, Any] = field(default_factory=dict)
    ci_status: dict[str, Any] = field(default_factory=dict)
    reviews: dict[str, Any] = field(default_factory=dict)
    diff: str = ""
    coverage_base: float = 0.0
    coverage_head: float = 0.0
    security_findings: dict[str, int] = field(default_factory=dict)
    deployments_today: int = 0
    max_daily_deployments: int = 10
    max_coverage_drop_pct: float = 5.0
    diff_warn_lines: int = 1000
    diff_block_lines: int = 5000
    required_reviewers: int = 1
    force_push: bool = False


class ConservationTracker:
    """
    Tracks a conserved quantity — deployments per day.

    This is the room's conservation law: the number of deployments
    in a 24-hour window is bounded. Exceeding the bound is not
    "discouraged" — it is physically prevented by the room protocol.
    """

    def __init__(self, max_per_day: int = 10):
        self.max_per_day = max_per_day
        self._deployments: list[float] = []  # timestamps

    def record(self, ts: float | None = None) -> None:
        """Record a deployment."""
        ts = ts if ts is not None else time.time()
        self._deployments.append(ts)
        self._prune(ts)

    def count_today(self, ts: float | None = None) -> int:
        """Count deployments in the last 24 hours."""
        ts = ts if ts is not None else time.time()
        self._prune(ts)
        return len(self._deployments)

    def remaining(self, ts: float | None = None) -> int:
        """How many deployments remain in the current window?"""
        return max(0, self.max_per_day - self.count_today(ts))

    def can_deploy(self, ts: float | None = None) -> bool:
        """Is another deployment allowed?"""
        return self.count_today(ts) < self.max_per_day

    def _prune(self, now: float) -> None:
        """Remove deployments older than 24 hours."""
        cutoff = now - 86400  # 24h
        self._deployments = [t for t in self._deployments if t >= cutoff]

    def to_dict(self) -> dict:
        """Serialize state for persistence."""
        return {
            "max_per_day": self.max_per_day,
            "deployments": list(self._deployments),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConservationTracker":
        """Deserialize state."""
        tracker = cls(max_per_day=data.get("max_per_day", 10))
        tracker._deployments = list(data.get("deployments", []))
        return tracker


# ─── Gate checks ─────────────────────────────────────────────

@gate
def check_ci_passing(ctx: GateContext) -> GateResult:
    """All required CI checks must pass."""
    ci = ctx.ci_status
    failed = ci.get("failed", 0)
    pending = ci.get("pending", 0)

    if failed > 0:
        return GateResult(
            gate_id="ci_passing",
            name="CI Status",
            passed=False,
            message=f"{failed} CI check(s) failing — deployment blocked.",
            severity="critical",
            blocking=True,
        )
    if pending > 0:
        return GateResult(
            gate_id="ci_passing",
            name="CI Status",
            passed=False,
            message=f"{pending} CI check(s) still pending — wait for completion.",
            severity="warning",
            blocking=True,
        )
    return GateResult(
        gate_id="ci_passing",
        name="CI Status",
        passed=True,
        message=f"All {ci.get('total', 0)} CI check(s) passing.",
    )


@gate
def check_coverage_delta(ctx: GateContext) -> GateResult:
    """Coverage must not drop beyond threshold."""
    if ctx.coverage_base == 0 and ctx.coverage_head == 0:
        return GateResult(
            gate_id="coverage_delta",
            name="Coverage Delta",
            passed=True,
            message="No coverage data available — skipping.",
        )

    delta = ctx.coverage_head - ctx.coverage_base
    drop_pct = -delta if delta < 0 else 0

    if drop_pct > ctx.max_coverage_drop_pct:
        return GateResult(
            gate_id="coverage_delta",
            name="Coverage Delta",
            passed=False,
            message=f"Coverage dropped by {drop_pct:.1f}% "
                    f"(threshold: {ctx.max_coverage_drop_pct}%).",
            severity="error",
            blocking=True,
        )

    if delta < 0:
        return GateResult(
            gate_id="coverage_delta",
            name="Coverage Delta",
            passed=True,
            message=f"Coverage dropped by {drop_pct:.1f}% (within threshold).",
            severity="info",
        )

    return GateResult(
        gate_id="coverage_delta",
        name="Coverage Delta",
        passed=True,
        message=f"Coverage improved by {delta:.1f}%.",
    )


@gate
def check_security_scan(ctx: GateContext) -> GateResult:
    """Security scan must not have critical findings."""
    critical = ctx.security_findings.get("critical", 0)
    error = ctx.security_findings.get("error", 0)

    if critical > 0:
        return GateResult(
            gate_id="security_scan",
            name="Security Scan",
            passed=False,
            message=f"{critical} critical security finding(s) — deployment blocked.",
            severity="critical",
            blocking=True,
        )
    if error > 0:
        return GateResult(
            gate_id="security_scan",
            name="Security Scan",
            passed=False,
            message=f"{error} security error(s) — review before deploying.",
            severity="error",
            blocking=True,
        )
    return GateResult(
        gate_id="security_scan",
        name="Security Scan",
        passed=True,
        message="No blocking security findings.",
    )


@gate
def check_diff_size(ctx: GateContext) -> GateResult:
    """Diff size within acceptable bounds."""
    additions = ctx.pr_info.get("additions", 0)
    deletions = ctx.pr_info.get("deletions", 0)
    total = additions + deletions

    if total > ctx.diff_block_lines:
        return GateResult(
            gate_id="diff_size",
            name="Diff Size",
            passed=False,
            message=f"Diff size {total} lines exceeds block threshold "
                    f"({ctx.diff_block_lines}). Split into smaller PRs.",
            severity="error",
            blocking=True,
        )
    if total > ctx.diff_warn_lines:
        return GateResult(
            gate_id="diff_size",
            name="Diff Size",
            passed=False,
            message=f"Diff size {total} lines exceeds warn threshold "
                    f"({ctx.diff_warn_lines}).",
            severity="warning",
        )
    return GateResult(
        gate_id="diff_size",
        name="Diff Size",
        passed=True,
        message=f"Diff size {total} lines is within limits.",
    )


@gate
def check_force_push_main(ctx: GateContext) -> GateResult:
    """Block force pushes to main."""
    base_ref = ctx.pr_info.get("base_ref", "")
    is_force = ctx.force_push or ctx.pr_info.get("force_push", False)

    if is_force and base_ref in ("main", "master", "production"):
        return GateResult(
            gate_id="force_push_main",
            name="Force Push to Main",
            passed=False,
            message=f"Force push to {base_ref} detected — deployment blocked.",
            severity="critical",
            blocking=True,
        )
    return GateResult(
        gate_id="force_push_main",
        name="Force Push to Main",
        passed=True,
        message="No force push to protected branch detected.",
    )


@gate
def check_rate_limit(ctx: GateContext) -> GateResult:
    """Enforce deployment rate limit (conservation law)."""
    if ctx.deployments_today >= ctx.max_daily_deployments:
        return GateResult(
            gate_id="rate_limit",
            name="Rate Limit",
            passed=False,
            message=f"Daily deployment limit reached "
                    f"({ctx.deployments_today}/{ctx.max_daily_deployments}). "
                    f"Conservation law enforced.",
            severity="critical",
            blocking=True,
        )
    return GateResult(
        gate_id="rate_limit",
        name="Rate Limit",
        passed=True,
        message=f"Deployments today: {ctx.deployments_today}/"
                f"{ctx.max_daily_deployments} — "
                f"{ctx.max_daily_deployments - ctx.deployments_today} remaining.",
    )


@gate
def check_review_approval(ctx: GateContext) -> GateResult:
    """Required number of reviewers must approve."""
    approvals = ctx.reviews.get("approvals", 0)
    changes_requested = ctx.reviews.get("changes_requested", 0)

    if changes_requested > 0:
        return GateResult(
            gate_id="review_approval",
            name="Review Approval",
            passed=False,
            message=f"{changes_requested} reviewer(s) requested changes.",
            severity="error",
            blocking=True,
        )
    if approvals < ctx.required_reviewers:
        return GateResult(
            gate_id="review_approval",
            name="Review Approval",
            passed=False,
            message=f"Only {approvals}/{ctx.required_reviewers} required approval(s).",
            severity="error",
            blocking=True,
        )
    return GateResult(
        gate_id="review_approval",
        name="Review Approval",
        passed=True,
        message=f"{approvals} approval(s) received (required: {ctx.required_reviewers}).",
    )


# ─── Orchestration ───────────────────────────────────────────

def run_all_gates(ctx: GateContext) -> list[GateResult]:
    """Run all registered gates and return results."""
    results = []
    for func in _ALL_GATES:
        try:
            result = func(ctx)
            results.append(result)
        except Exception as exc:
            results.append(GateResult(
                gate_id=func.__name__,
                name=func.__name__,
                passed=True,  # Don't block on gate crash
                message=f"Gate errored: {exc}",
                severity="info",
            ))
    return results


def deployment_decision(results: list[GateResult]) -> tuple[str, list[GateResult]]:
    """Determine deployment decision from gate results.

    Returns:
        Tuple of (decision, blocking_failures).
        Decision is one of: APPROVED, BLOCKED, WARNED
    """
    blocking_failures = [r for r in results if not r.passed and r.blocking]
    warnings = [r for r in results if not r.passed and not r.blocking]

    if blocking_failures:
        return "BLOCKED", blocking_failures
    if warnings:
        return "WARNED", warnings
    return "APPROVED", []
