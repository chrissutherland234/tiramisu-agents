"""Bundled fictional deployment used by local development and contract tests."""

from tiramisu_agents.builtin.fictional import (
    FictionalDeployment,
    load_fictional_deployment,
)

__all__ = ["FictionalDeployment", "load_fictional_deployment"]
