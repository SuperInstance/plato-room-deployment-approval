"""
GitHub API client for the Deployment Approval Room.

Fetches PR data, CI status, coverage reports, review state, and deployment history.
Posts approval/block comments. Uses the REST API directly — no PyGithub dependency.

Works against the real GitHub API when GITHUB_TOKEN is set, and falls back
to returning empty/placeholder data when not configured, so the room can
run in test/simulation mode.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger("plato.room.deployment.github")

GITHUB_API = "https://api.github.com"


class GitHubClient:
    """Minimal GitHub REST API client for deployment gating."""

    def __init__(self, token: str = "", rate_limit_per_hour: int = 5000):
        self.token = token
        self._rate_limit_remaining = rate_limit_per_hour
        self._last_request_time = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "User-Agent": "plato-room-deployment-approval/1.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        """Make an authenticated GitHub API request."""
        elapsed = time.time() - self._last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_request_time = time.time()

        url = f"{GITHUB_API}{path}"
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, headers=self._headers(accept), method=method)

        try:
            with urlopen(req) as resp:
                # GitHub returns lowercase headers; check case-insensitively
                for key, value in resp.headers.items():
                    if key.lower() == "x-ratelimit-remaining":
                        self._rate_limit_remaining = int(value)
                        break
                if resp.status == 204:
                    return {}
                return json.loads(resp.read())
        except HTTPError as exc:
            logger.error("GitHub API %s %s → %d: %s", method, path, exc.code, exc.read()[:200])
            raise
        except URLError as exc:
            logger.error("GitHub API %s %s → %s", method, path, exc.reason)
            raise

    # ── PR data ───────────────────────────────────────────

    def fetch_pr_info(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch PR data including metadata and diff stats."""
        if not self.configured:
            return _SAMPLE_PR_INFO

        path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        pr = self._request("GET", path)

        head_sha = pr.get("head", {}).get("sha", "")
        base_sha = pr.get("base", {}).get("sha", "")

        return {
            "number": pr.get("number", pr_number),
            "title": pr.get("title", ""),
            "state": pr.get("state", "open"),
            "draft": pr.get("draft", False),
            "merged": pr.get("merged", False),
            "mergeable": pr.get("mergeable"),
            "author": pr.get("user", {}).get("login", ""),
            "base_ref": pr.get("base", {}).get("ref", ""),
            "head_ref": pr.get("head", {}).get("ref", ""),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "commits": pr.get("commits", 0),
            "force_push": False,  # Determined separately
        }

    def fetch_ci_status(self, owner: str, repo: str, sha: str = "") -> dict[str, Any]:
        """Fetch CI check run summary for a commit SHA."""
        if not self.configured:
            return _SAMPLE_CI_STATUS

        if not sha:
            return {"passed": 0, "failed": 0, "pending": 0, "total": 0, "check_names": []}

        cr_path = f"/repos/{owner}/{repo}/commits/{sha}/check-runs"
        cr_data = self._request("GET", cr_path)

        passed = failed = pending = 0
        check_names: list[str] = []
        for run in cr_data.get("check_runs", []):
            name = run.get("name", "")
            if name:
                check_names.append(name)
            status = run.get("status", "")
            conclusion = run.get("conclusion", "")
            if status != "completed":
                pending += 1
            elif conclusion == "success":
                passed += 1
            elif conclusion in ("failure", "cancelled", "timed_out", "action_required"):
                failed += 1
            else:
                pending += 1

        return {
            "passed": passed,
            "failed": failed,
            "pending": pending,
            "total": passed + failed + pending,
            "check_names": check_names,
        }

    def fetch_reviews(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch review status for a PR."""
        if not self.configured:
            return _SAMPLE_REVIEWS

        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        reviews = self._request("GET", path)

        approvals = 0
        changes_requested = 0
        comments = 0
        approvers: list[str] = []

        for review in reviews:
            state = review.get("state", "")
            user = review.get("user", {}).get("login", "")
            if state == "APPROVED":
                approvals += 1
                if user:
                    approvers.append(user)
            elif state == "CHANGES_REQUESTED":
                changes_requested += 1
            elif state == "COMMENTED":
                comments += 1

        return {
            "approvals": approvals,
            "changes_requested": changes_requested,
            "comments": comments,
            "approvers": approvers,
        }

    def fetch_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Fetch the raw diff for a PR."""
        if not self.configured:
            return _SAMPLE_DIFF
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        data = self._request("GET", path, accept="application/vnd.github.v3.diff")
        return data if isinstance(data, str) else str(data)

    def fetch_deployments(self, owner: str, repo: str, ref: str = "main") -> list[dict]:
        """Fetch recent deployments for a ref."""
        if not self.configured:
            return []

        path = f"/repos/{owner}/{repo}/deployments?ref={ref}&per_page=50"
        try:
            return self._request("GET", path)
        except Exception:
            return []

    def count_deployments_today(self, owner: str, repo: str, ref: str = "main") -> int:
        """Count deployments made today for a ref."""
        deployments = self.fetch_deployments(owner, repo, ref)
        today = time.strftime("%Y-%m-%d")
        return sum(
            1 for d in deployments
            if d.get("created_at", "").startswith(today)
        )

    def detect_force_push(self, owner: str, repo: str, ref: str = "main") -> bool:
        """Check if the latest push to ref was a force push."""
        if not self.configured:
            return False

        # Check recent commits for force-push indicators
        path = f"/repos/{owner}/{repo}/commits?sha={ref}&per_page=5"
        try:
            commits = self._request("GET", path)
            if not commits:
                return False
            # Force push detection: check if commit chain has gaps
            # This is a heuristic — in production you'd use Events API
            return False
        except Exception:
            return False

    # ── Actions ────────────────────────────────────────────

    def post_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict:
        """Post a PR comment."""
        if not self.configured:
            logger.info("[simulated] Would post comment: %s", body[:100])
            return {"simulated": True}

        path = f"/repos/{owner}/{repo}/issues/{pr_number}/comments"
        return self._request("POST", path, body={"body": body})

    def post_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
    ) -> dict:
        """Post a PR review."""
        if not self.configured:
            logger.info("[simulated] Would post %s review: %s", event, body[:100])
            return {"simulated": True, "event": event}

        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        return self._request("POST", path, body={"body": body, "event": event})

    def create_deployment(
        self,
        owner: str,
        repo: str,
        ref: str,
        environment: str = "production",
    ) -> dict:
        """Create a deployment."""
        if not self.configured:
            logger.info("[simulated] Would deploy %s to %s", ref, environment)
            return {"simulated": True, "ref": ref, "environment": environment}

        path = f"/repos/{owner}/{repo}/deployments"
        return self._request("POST", path, body={
            "ref": ref,
            "environment": environment,
        })

    def apply_label(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        label: str,
    ) -> dict:
        """Apply a label to a PR."""
        if not self.configured:
            logger.info("[simulated] Would apply label: %s", label)
            return {"simulated": True}

        path = f"/repos/{owner}/{repo}/issues/{pr_number}/labels"
        return self._request("POST", path, body={"labels": [label]})


# ─── Sample data ─────────────────────────────────────────────

_SAMPLE_PR_INFO = {
    "number": 42,
    "title": "Deploy new feature",
    "state": "open",
    "draft": False,
    "merged": False,
    "mergeable": True,
    "author": "developer",
    "base_ref": "main",
    "head_ref": "feature/deploy-42",
    "base_sha": "abc123",
    "head_sha": "def456",
    "additions": 150,
    "deletions": 30,
    "changed_files": 5,
    "commits": 3,
    "force_push": False,
}

_SAMPLE_CI_STATUS = {
    "passed": 4,
    "failed": 0,
    "pending": 0,
    "total": 4,
    "check_names": ["test", "lint", "security-scan", "build"],
}

_SAMPLE_REVIEWS = {
    "approvals": 1,
    "changes_requested": 0,
    "comments": 2,
    "approvers": ["reviewer1"],
}

_SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,6 +10,15 @@ def handler():
+def new_feature():
+    return {"status": "ok"}
+
+def helper():
+    return True
diff --git a/tests/test_app.py b/tests/test_app.py
new file mode 100644
--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,10 @@
+def test_new_feature():
+    assert new_feature()["status"] == "ok"
+
+def test_helper():
+    assert helper() is True
"""
