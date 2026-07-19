"""
PLATO Room Deployment Approval — package init.
"""

# Support both package-relative and standalone imports
try:
    from .room import DeploymentApprovalRoom, BaseRoom
    from .gate import run_all_gates, GateResult, ConservationTracker
    from .github_client import GitHubClient
    from .report import generate_report, generate_short_summary
except ImportError:
    from room import DeploymentApprovalRoom, BaseRoom
    from gate import run_all_gates, GateResult, ConservationTracker
    from github_client import GitHubClient
    from report import generate_report, generate_short_summary

__version__ = "0.1.1"
__all__ = [
    "DeploymentApprovalRoom",
    "BaseRoom",
    "run_all_gates",
    "GateResult",
    "ConservationTracker",
    "GitHubClient",
    "generate_report",
    "generate_short_summary",
]
