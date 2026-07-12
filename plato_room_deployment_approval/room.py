"""
PLATO Deployment Approval Room — an engine block that gates deployments.

Sensors pull CI status, coverage data, review state, and deployment history.
Alarms fire on policy violations. Actuators approve or block deployments.
The conservation tracker enforces the deployment rate limit.

Usage:
    python -m plato_room_deployment_approval.room --repo owner/repo --pr 42

    # or run as a long-lived server
    python -m plato_room_deployment_approval.room --repo owner/repo --watch
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import socketserver
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

try:
    from plato_core.protocol import PROTOCOL_VERSION
except ImportError:
    PROTOCOL_VERSION = "0.1"

try:
    from .gate import (
        run_all_gates, deployment_decision, GateResult, GateContext,
        ConservationTracker,
    )
    from .github_client import GitHubClient
    from .report import generate_report, generate_short_summary
except ImportError:
    from gate import (
        run_all_gates, deployment_decision, GateResult, GateContext,
        ConservationTracker,
    )
    from github_client import GitHubClient
    from report import generate_report, generate_short_summary

logger = logging.getLogger("plato.room.deployment")

# ─── Core room primitives ─────────────────────────────────────

SensorFunc = Callable[["BaseRoom"], dict[str, float]]
ActuatorFunc = Callable[["BaseRoom", float], None]


@dataclass
class AlarmDef:
    """Declarative alarm definition."""
    alarm_id: str
    condition: str
    sensor: str
    operator: str
    threshold: float
    cooldown_sec: int = 300
    last_triggered: float = 0.0
    state: str = "idle"

    def evaluate(self, sensor_values: dict[str, float]) -> bool:
        val = sensor_values.get(self.sensor)
        if val is None:
            return False
        ops = {
            "<":  lambda a, b: a < b,
            ">":  lambda a, b: a > b,
            "<=": lambda a, b: a <= b,
            ">=": lambda a, b: a >= b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
        }
        op = ops.get(self.operator)
        if op is None:
            return False
        return op(val, self.threshold)


@dataclass
class ErrorResponse:
    type: str = "error"
    message: str = ""


class BaseRoom:
    """
    Base PLATO room — a TCP server implementing the wire protocol.

    Subclasses register sensors, actuators, and alarms in setup().
    """

    tick_hz: float = 0.2
    room_id: str = "base-room"

    def __init__(self, host: str = "0.0.0.0", port: int = 1236):
        self.host = host
        self.port = port
        self._sensors: dict[str, SensorFunc] = {}
        self._actuators: dict[str, ActuatorFunc] = {}
        self._alarms: dict[str, AlarmDef] = {}
        self._history: deque[dict] = deque(maxlen=1000)
        self._audit_log: deque[dict] = deque(maxlen=10000)  # deployment audit trail
        self._subscribers: list[socket.socket] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._running = False
        self._latest: dict[str, float] = {}

    def register_sensor(self, name: str, func: SensorFunc) -> None:
        self._sensors[name] = func

    def register_actuator(self, name: str, func: ActuatorFunc) -> None:
        self._actuators[name] = func

    def register_alarm(
        self,
        alarm_id: str,
        sensor: str,
        operator: str,
        threshold: float,
        cooldown_sec: int = 300,
    ) -> None:
        condition = f"{sensor} {operator} {threshold}"
        self._alarms[alarm_id] = AlarmDef(
            alarm_id=alarm_id,
            condition=condition,
            sensor=sensor,
            operator=operator,
            threshold=threshold,
            cooldown_sec=cooldown_sec,
        )

    def log_audit(self, event: str, data: dict) -> None:
        """Log an event to the audit trail."""
        entry = {
            "t": time.time(),
            "event": event,
            "data": data,
        }
        self._audit_log.append(entry)
        logger.info("AUDIT: %s — %s", event, data)

    def tick(self) -> dict[str, float]:
        """Execute one tick: read sensors, evaluate alarms."""
        self._seq += 1
        values: dict[str, float] = {}
        for name, func in self._sensors.items():
            try:
                result = func(self)
                if isinstance(result, dict):
                    values.update(result)
            except Exception as exc:
                logger.warning("sensor %s error: %s", name, exc)
                values[f"{name}_error"] = 1.0

        self._latest = values
        ts = time.time()
        tick_record = {"t": ts, "seq": self._seq, "data": values}
        self._history.append(tick_record)

        for alarm in self._alarms.values():
            if alarm.evaluate(values):
                if ts - alarm.last_triggered >= alarm.cooldown_sec:
                    alarm.last_triggered = ts
                    alarm.state = "triggered"
                    self._on_alarm(alarm, values, ts)
                else:
                    alarm.state = "cooling"
            else:
                alarm.state = "idle"

        self._notify_subscribers(tick_record)
        return values

    def _on_alarm(self, alarm: AlarmDef, data: dict[str, float], ts: float) -> None:
        logger.info("ALARM %s fired: %s", alarm.alarm_id, alarm.condition)

    def _notify_subscribers(self, tick_record: dict) -> None:
        msg = json.dumps({"type": "tick", **tick_record}) + "\n"
        dead: list[socket.socket] = []
        with self._lock:
            for sub in self._subscribers:
                try:
                    sub.sendall(msg.encode())
                except Exception:
                    dead.append(sub)
            for d in dead:
                self._subscribers.remove(d)

    def actuate(self, name: str, value: float = 1.0) -> None:
        func = self._actuators.get(name)
        if func:
            func(self, value)
        else:
            logger.warning("unknown actuator: %s", name)

    def handle_command(self, line: str) -> str:
        """Handle one protocol command line."""
        parts = line.strip().split()
        if not parts:
            return json.dumps(asdict(ErrorResponse(message="empty command")))

        cmd = parts[0]

        if cmd == "tick":
            data = self.tick()
            return json.dumps({"type": "tick", "t": time.time(),
                               "seq": self._seq, "data": data})

        if cmd == "history":
            n = int(parts[1]) if len(parts) > 1 else 10
            ticks = list(self._history)[-n:]
            return json.dumps({"type": "history", "count": len(ticks),
                               "ticks": ticks})

        if cmd == "audit":
            n = int(parts[1]) if len(parts) > 1 else 10
            entries = list(self._audit_log)[-n:]
            return json.dumps({"type": "audit", "count": len(entries),
                               "entries": entries})

        if cmd == "actuator" and len(parts) >= 2:
            name = parts[1]
            value = float(parts[2]) if len(parts) > 2 else 1.0
            self.actuate(name, value)
            return json.dumps({"type": "ack", "command": "actuator",
                               "name": name, "value": value})

        if cmd == "alarm" and len(parts) >= 2:
            sub = parts[1]
            if sub == "list":
                alarms = [
                    {"id": a.alarm_id, "condition": a.condition,
                     "cooldown_sec": a.cooldown_sec,
                     "last_triggered": a.last_triggered,
                     "state": a.state}
                    for a in self._alarms.values()
                ]
                return json.dumps({"type": "alarm_list", "alarms": alarms})
            if sub == "set" and len(parts) >= 5:
                aid = parts[2]
                condition = parts[3]
                cooldown = int(parts[4])
                tokens = condition.split()
                if len(tokens) == 3:
                    self.register_alarm(aid, tokens[0], tokens[1],
                                        float(tokens[2]), cooldown)
                return json.dumps({"type": "ack", "command": "alarm set",
                                   "id": aid})

        if cmd == "subscribe":
            return json.dumps({"type": "subscribed", "tick_hz": self.tick_hz})

        if cmd == "unsubscribe":
            return json.dumps({"type": "unsubscribed"})

        if cmd == "help":
            return json.dumps({"type": "help", "commands": [
                "tick", "history N", "audit N",
                "actuator NAME [VALUE]",
                "alarm list", "alarm set ID CONDITION COOLDOWN",
                "subscribe", "unsubscribe", "help", "quit",
            ]})

        if cmd == "quit":
            return json.dumps({"type": "bye"})

        return json.dumps(asdict(ErrorResponse(
            message=f"unknown command: {cmd}")))

    def serve(self) -> None:
        self._running = True

        tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        tick_thread.start()

        server = socketserver.ThreadingTCPServer(
            (self.host, self.port), _RoomHandlerFactory(self))
        server.daemon_threads = True
        logger.info("Room %s listening on %s:%d", self.room_id, self.host, self.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self._running = False
            server.shutdown()

    def _tick_loop(self) -> None:
        interval = 1.0 / self.tick_hz if self.tick_hz > 0 else 5.0
        while self._running:
            try:
                self.tick()
            except Exception as exc:
                logger.error("tick error: %s", exc)
            time.sleep(interval)


def _RoomHandlerFactory(room: BaseRoom):
    """Create a RequestHandler class bound to a specific room."""

    class _RoomHandler(socketserver.StreamRequestHandler):
        def handle(self):
            welcome = json.dumps({
                "type": "welcome",
                "room_id": room.room_id,
                "tick_hz": room.tick_hz,
                "sensors": list(room._sensors.keys()),
                "format": "json",
                "protocol_version": PROTOCOL_VERSION,
            }) + "\n"
            self.wfile.write(welcome.encode())

            for line in self.rfile:
                line_str = line.decode().strip()
                if not line_str:
                    continue
                response = room.handle_command(line_str)
                self.wfile.write((response + "\n").encode())

                if line_str.startswith("subscribe"):
                    with room._lock:
                        room._subscribers.append(self.request)
                elif line_str.startswith("unsubscribe"):
                    with room._lock:
                        if self.request in room._subscribers:
                            room._subscribers.remove(self.request)
                elif line_str.strip() == "quit":
                    break

    return _RoomHandler


# ─── Deployment Approval Room ────────────────────────────────

class DeploymentApprovalRoom(BaseRoom):
    """
    A PLATO room that gates deployments.

    Sensors:
        ci_status          — CI passed/failed/pending counts
        coverage_delta     — Coverage change vs. base branch
        security_status    — Security scan results
        deployment_state   — Deployments today, rate limit remaining
        review_state       — Review approvals/changes_requested

    Actuators:
        approve_deployment  — Post approval and create deployment
        block_deployment    — Post block comment
        post_approval       — Post approval comment
        trigger_rollback    — Trigger rollback (label for manual action)

    Alarms:
        coverage_drop       — fires when coverage_delta < -5.0
        ci_failing          — fires when ci_failed > 0
        force_push_main     — fires when force_push detected on main
        rate_limit_exceeded — fires when daily_count >= max_daily

    Conservation:
        daily_deployments   — Max N deployments per day (enforced)
    """

    room_id = "deployment-approval-room"
    tick_hz = 0.1

    def __init__(
        self,
        github_client: GitHubClient,
        owner: str,
        repo: str,
        pr_number: int = 0,
        host: str = "0.0.0.0",
        port: int = 1236,
        max_daily_deployments: int = 10,
        max_coverage_drop_pct: float = 5.0,
        required_reviewers: int = 1,
    ):
        super().__init__(host=host, port=port)
        self.gh = github_client
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.conservation = ConservationTracker(max_per_day=max_daily_deployments)
        self.max_coverage_drop_pct = max_coverage_drop_pct
        self.required_reviewers = required_reviewers
        self._last_decision: Optional[str] = None
        self.setup()

    def setup(self) -> None:
        """Register sensors, actuators, and alarms."""
        # ── Sensors ─────────────────────────────────────────
        self.register_sensor("ci_status", _sensor_ci_status)
        self.register_sensor("coverage_delta", _sensor_coverage_delta)
        self.register_sensor("security_status", _sensor_security_status)
        self.register_sensor("deployment_state", _sensor_deployment_state)
        self.register_sensor("review_state", _sensor_review_state)
        self.register_sensor("diff_metrics", _sensor_diff_metrics)

        # ── Actuators ───────────────────────────────────────
        self.register_actuator("approve_deployment", _actuator_approve)
        self.register_actuator("block_deployment", _actuator_block)
        self.register_actuator("post_approval", _actuator_post_approval)
        self.register_actuator("trigger_rollback", _actuator_rollback)

        # ── Alarms ──────────────────────────────────────────
        self.register_alarm("coverage_drop",
                            sensor="coverage_delta_pct",
                            operator="<", threshold=-self.max_coverage_drop_pct,
                            cooldown_sec=300)
        self.register_alarm("ci_failing",
                            sensor="ci_failed",
                            operator=">", threshold=0,
                            cooldown_sec=120)
        self.register_alarm("force_push_main",
                            sensor="force_push_detected",
                            operator=">", threshold=0,
                            cooldown_sec=60)
        self.register_alarm("rate_limit_exceeded",
                            sensor="daily_count",
                            operator=">=", threshold=float(self.conservation.max_per_day),
                            cooldown_sec=3600)

    def _on_alarm(self, alarm: AlarmDef, data: dict[str, float], ts: float) -> None:
        """Handle deployment alarms."""
        logger.warning("Deployment alarm %s triggered (data=%s)", alarm.alarm_id, data)
        self.log_audit("alarm", {
            "alarm_id": alarm.alarm_id,
            "condition": alarm.condition,
            "data": data,
        })

        if alarm.alarm_id in ("ci_failing", "force_push_main", "rate_limit_exceeded"):
            self.actuate("block_deployment", 1.0)
        elif alarm.alarm_id == "coverage_drop":
            self.actuate("post_approval", 2.0)  # Post warning comment


# ─── Sensor functions ────────────────────────────────────────

def _sensor_ci_status(room: DeploymentApprovalRoom) -> dict[str, float]:
    """Fetch CI check status."""
    if not room.pr_number:
        return {"ci_passed": 0.0, "ci_failed": 0.0, "ci_pending": 0.0}

    pr_info = room.gh.fetch_pr_info(room.owner, room.repo, room.pr_number)
    head_sha = pr_info.get("head_sha", "")
    ci = room.gh.fetch_ci_status(room.owner, room.repo, head_sha)

    return {
        "ci_passed": float(ci.get("passed", 0)),
        "ci_failed": float(ci.get("failed", 0)),
        "ci_pending": float(ci.get("pending", 0)),
    }


def _sensor_coverage_delta(room: DeploymentApprovalRoom) -> dict[str, float]:
    """Calculate coverage delta (placeholder — uses sample data)."""
    # In production, this would fetch coverage from CI artifacts
    return {
        "coverage_base": 80.0,
        "coverage_head": 82.5,
        "coverage_delta_pct": 2.5,
    }


def _sensor_security_status(room: DeploymentApprovalRoom) -> dict[str, float]:
    """Check security scan results (placeholder)."""
    # In production, this would fetch from plato-room-security-audit
    return {
        "security_critical": 0.0,
        "security_error": 0.0,
    }


def _sensor_deployment_state(room: DeploymentApprovalRoom) -> dict[str, float]:
    """Check deployment rate and conservation state."""
    count = room.conservation.count_today()
    max_daily = room.conservation.max_per_day
    remaining = room.conservation.remaining()

    # Check for force push
    pr_info = room.gh.fetch_pr_info(room.owner, room.repo, room.pr_number)
    force_push = 1.0 if pr_info.get("force_push", False) else 0.0
    base_ref = pr_info.get("base_ref", "")
    if base_ref in ("main", "master") and force_push:
        force_push = 1.0
    else:
        force_push = 0.0

    return {
        "daily_count": float(count),
        "daily_max": float(max_daily),
        "daily_remaining": float(remaining),
        "force_push_detected": force_push,
    }


def _sensor_review_state(room: DeploymentApprovalRoom) -> dict[str, float]:
    """Fetch review approval state."""
    if not room.pr_number:
        return {"review_approvals": 0.0, "review_changes_requested": 0.0}

    reviews = room.gh.fetch_reviews(room.owner, room.repo, room.pr_number)
    return {
        "review_approvals": float(reviews.get("approvals", 0)),
        "review_changes_requested": float(reviews.get("changes_requested", 0)),
    }


def _sensor_diff_metrics(room: DeploymentApprovalRoom) -> dict[str, float]:
    """Fetch diff size metrics."""
    if not room.pr_number:
        return {"diff_additions": 0.0, "diff_deletions": 0.0, "diff_total": 0.0}

    pr_info = room.gh.fetch_pr_info(room.owner, room.repo, room.pr_number)
    additions = pr_info.get("additions", 0)
    deletions = pr_info.get("deletions", 0)
    return {
        "diff_additions": float(additions),
        "diff_deletions": float(deletions),
        "diff_total": float(additions + deletions),
    }


# ─── Actuator functions ──────────────────────────────────────

def _actuator_approve(room: DeploymentApprovalRoom, value: float) -> None:
    """Approve a deployment."""
    if not room.conservation.can_deploy():
        logger.warning("Deployment blocked by conservation law")
        room.log_audit("blocked", {"reason": "rate_limit_exceeded"})
        return

    room.conservation.record()
    room.gh.post_comment(
        room.owner, room.repo, room.pr_number,
        "✅ **Deployment Approved** by PLATO Deployment Approval Room.\n"
        f"Deployments today: {room.conservation.count_today()}/"
        f"{room.conservation.max_per_day}",
    )
    room.gh.apply_label(room.owner, room.repo, room.pr_number, "deployment-approved")
    room._last_decision = "APPROVED"
    room.log_audit("approved", {
        "pr": room.pr_number,
        "deployments_today": room.conservation.count_today(),
    })


def _actuator_block(room: DeploymentApprovalRoom, value: float) -> None:
    """Block a deployment."""
    reason = "Gate check failed" if value == 1.0 else "Conservation limit reached"

    room.gh.post_comment(
        room.owner, room.repo, room.pr_number,
        f"🚫 **Deployment Blocked** by PLATO Deployment Approval Room.\n"
        f"Reason: {reason}",
    )
    room.gh.apply_label(room.owner, room.repo, room.pr_number, "deployment-blocked")
    room._last_decision = "BLOCKED"
    room.log_audit("blocked", {
        "pr": room.pr_number,
        "reason": reason,
    })


def _actuator_post_approval(room: DeploymentApprovalRoom, value: float) -> None:
    """Post approval-related comment. value: 1=approve, 2=warn."""
    if value == 1.0:
        room.gh.post_comment(
            room.owner, room.repo, room.pr_number,
            "🚦 **Deployment Review** — All gates passed.",
        )
    elif value == 2.0:
        room.gh.post_comment(
            room.owner, room.repo, room.pr_number,
            "⚠️ **Deployment Warning** — Some gates have warnings. Review before deploying.",
        )


def _actuator_rollback(room: DeploymentApprovalRoom, value: float) -> None:
    """Trigger rollback by labeling for manual action."""
    room.gh.apply_label(room.owner, room.repo, room.pr_number, "needs-rollback")
    room.gh.post_comment(
        room.owner, room.repo, room.pr_number,
        "🔄 **Rollback Triggered** — Previous deployment needs rollback.",
    )
    room.log_audit("rollback", {"pr": room.pr_number})


# ─── CLI ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PLATO Deployment Approval Room")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, default=0, help="PR number to gate")
    parser.add_argument("--watch", action="store_true", help="Run as long-lived server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=1236)
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="Env var for GitHub token")
    parser.add_argument("--once", action="store_true", help="Evaluate once and exit")
    parser.add_argument("--max-daily", type=int, default=10, help="Max deployments per day")
    args = parser.parse_args()

    import os
    token = os.environ.get(args.token_env, "")
    owner, repo = args.repo.split("/")

    gh = GitHubClient(token)
    room = DeploymentApprovalRoom(
        gh, owner, repo, args.pr,
        args.host, args.port,
        max_daily_deployments=args.max_daily,
    )

    if args.once and args.pr:
        pr_info = gh.fetch_pr_info(owner, repo, args.pr)
        head_sha = pr_info.get("head_sha", "")
        ci_status = gh.fetch_ci_status(owner, repo, head_sha)
        reviews = gh.fetch_reviews(owner, repo, args.pr)

        ctx = GateContext(
            pr_info=pr_info,
            ci_status=ci_status,
            reviews=reviews,
            deployments_today=room.conservation.count_today(),
            max_daily_deployments=room.conservation.max_per_day,
        )
        results = run_all_gates(ctx)
        decision, blocking = deployment_decision(results)

        report = generate_report(
            results, decision=decision, blocking_failures=blocking,
            repo=args.repo, pr_number=args.pr, author=pr_info.get("author", ""),
            conservation=room.conservation,
        )
        print(report)
        return

    if args.watch:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        room.serve()
    else:
        print(f"Room {room.room_id} configured. Use --watch to start, --once to evaluate PR #{args.pr}")


if __name__ == "__main__":
    main()
