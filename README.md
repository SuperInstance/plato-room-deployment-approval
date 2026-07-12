# PLATO Deployment Approval Room

> Deployment gating as a **PLATO engine block** — the third room in the SuperInstance ecosystem.

[![Tests](https://github.com/SuperInstance/plato-room-deployment-approval/actions/workflows/ci.yml/badge.svg)](https://github.com/SuperInstance/plato-room-deployment-approval/actions/workflows/ci.yml)

## What is this?

A **PLATO Room** that gates deployments based on CI status, test coverage, security scans, and rate limits. It follows the PLATO architecture:

| Concept | In this room | |
|---------|-------------|---|
| **Sensors** | CI status, test coverage delta, security scan results, diff size, deployment count | Pull data from GitHub + CI on each tick |
| **Actuators** | Approve deployment, block deployment, post approval comment, trigger rollback | Push decisions back to GitHub |
| **Tick loop** | Configurable rate (default 0.1 Hz = every 10s) | Polls for deployment state changes |
| **Alarms** | `coverage_drop`, `ci_failing`, `force_push_main`, `rate_limit_exceeded` | Fire on policy violations |
| **Conservation** | Max deployments per day (rate limiting as conservation law) | Deployments are a conserved quantity |
| **History** | Ring buffer of last 1000 ticks — full audit trail of all decisions | Immutable deployment log |

The room exposes the standard **PLATO wire protocol** — any PLATO client can connect, read sensors, check alarms, and trigger actuators.

## Quick Start

### Standalone (single deployment check)

```bash
export GITHUB_TOKEN=ghp_your_token_here
python -m plato_room_deployment_approval.room --repo owner/repo --pr 42 --once
```

Prints a deployment approval decision and exits.

### Long-lived server

```bash
export GITHUB_TOKEN=ghp_your_token_here
python -m plato_room_deployment_approval.room --repo owner/repo --watch --port 1236
```

Then connect with any PLATO client:

```python
from plato_core.protocol import PlatoClient

with PlatoClient.connect("localhost", 1236) as client:
    welcome = client.recv_response()
    print(f"Connected to {welcome.room_id}")

    client.send("tick")
    tick = client.recv_response()
    print(f"Deployment state: {tick.data}")

    client.send("alarm list")
    alarms = client.recv_response()
    for a in alarms.alarms:
        print(f"  {a.id}: {a.state}")

    # Approve deployment
    client.send("actuator approve_deployment 1")
```

### GitHub Action

```yaml
# .github/workflows/deployment-gate.yml
name: PLATO Deployment Gate
on:
  pull_request:
    types: [opened, synchronize]
  push:
    branches: [main]

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install plato-core plato-room-deployment-approval
      - run: |
          python -m plato_room_deployment_approval.room \
            --repo ${{ github.repository }} \
            --pr ${{ github.event.pull_request.number }} \
            --once
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## Deployment Checks

All checks are **deterministic** — no LLM, no fuzzy logic. Fast and auditable.

| Check | What it evaluates | Outcome |
|-------|------------------|---------|
| `ci_passing` | All required CI check runs pass | BLOCK if failing |
| `coverage_delta` | Test coverage change vs. base branch | BLOCK if drop > threshold |
| `security_scan` | Security scan results (from audit room) | BLOCK if critical findings |
| `diff_size` | Total lines changed | WARN if > 1000, BLOCK if > 5000 |
| `force_push_main` | Force push to main branch | BLOCK immediately |
| `rate_limit` | Max deployments per day | BLOCK if exceeded |
| `review_approval` | Required reviewers approved | BLOCK if not approved |

### Conservation Law: Deployment Rate Limiting

Deployments are treated as a **conserved quantity** — the room enforces a maximum number of deployments per day (default: 10). This is the room's conservation law in action.

```python
# Configure rate limit
room.set_conservation_limit("daily_deployments", 5)  # Max 5 deploys/day

# The room tracks deployments and blocks when the limit is reached
# This is not a suggestion — it's enforced by the room protocol
```

## Configuration

Configure via `plato-deployment.yml` in your repo root:

```yaml
gates:
  ci_passing: enabled
  coverage_delta:
    max_drop_pct: 5.0  # Block if coverage drops by more than 5%
  security_scan: enabled
  diff_size:
    warn_lines: 1000
    block_lines: 5000
  force_push_main: enabled
  rate_limit:
    max_daily: 10
  review_approval:
    required_reviewers: 1
```

Options: `enabled`, `disabled`, `warn`.

## Architecture

```
                    PLATO Wire Protocol
                    (TCP, JSON lines)
                           │
          ┌────────────────┼────────────────┐
          │                │                │
     tick command     alarm list      actuator cmd
          │                │                │
          ▼                ▼                ▼
    ┌─────────────────────────────────────────────────┐
    │         Deployment Approval Room                │
    │                                                 │
    │  Sensors               Actuators                │
    │  ├─ ci_status          ├─ approve_deployment    │
    │  ├─ coverage_delta     ├─ block_deployment      │
    │  ├─ security_scan      ├─ post_approval         │
    │  ├─ diff_metrics       └─ trigger_rollback      │
    │  ├─ deployment_count   │                        │
    │  └─ review_status      │                        │
    │                                                 │
    │  Alarms                                        │
    │  ├─ coverage_drop (coverage_delta < -5.0)      │
    │  ├─ ci_failing (ci_failed > 0)                 │
    │  ├─ force_push_main (force_push == 1)          │
    │  └─ rate_limit_exceeded (daily_count >= max)   │
    │                                                 │
    │  Conservation                                  │
    │  └─ daily_deployments: max 10/day              │
    │                                                 │
    │  History (1000-tick ring buffer)                │
    │  └─ Full audit trail of all decisions           │
    └─────────────────────────────────────────────────┘
                           │
                    GitHub API
                           │
          ┌────────────────┼────────────────┐
          │                │                │
     Fetch CI status  Fetch coverage   Post approval
     Fetch PR info    Count deploys    Block merge
```

## Conservation Laws

This room demonstrates **conservation laws for AI agents** — a core SuperInstance principle:

- **Deployment rate** is conserved: the room physically cannot exceed `max_daily` deployments
- **Audit trail** is conserved: all decisions are logged immutably in the history buffer
- **Gate compliance** is conserved: no deployment can bypass the gate checks

## FLUX Policies

```flux
POLICY ci_must_pass {
    SENSE  ci_failed
    GUARD   ci_failed > 0
    ALARM   severity=critical
    ACTUATE block_deployment
    EMIT    "CI is failing — deployment blocked"
}

POLICY coverage_no_drop {
    SENSE  coverage_delta
    GUARD   coverage_delta < -5.0
    ALARM   severity=error
    ACTUATE block_deployment
    EMIT    "Coverage dropped below threshold"
}
```

## Testing

```bash
pip install -e ".[test]"
pytest tests/ -v
```

All tests use simulated data — no GitHub API calls needed.

## License

MIT — see [LICENSE](LICENSE).

## Part of

[SuperInstance](https://github.com/SuperInstance) — the PLATO ecosystem.
