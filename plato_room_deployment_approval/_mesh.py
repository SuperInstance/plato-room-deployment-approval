"""
Mesh registration for PLATO plugin discovery.
"""

def register(registry):
    """Register the deployment approval room with the PLATO mesh."""
    from .room import DeploymentApprovalRoom

    registry.register("rooms", "deployment_approval", lambda: DeploymentApprovalRoom)
