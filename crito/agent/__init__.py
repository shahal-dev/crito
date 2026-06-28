"""Site Agent: owns device connections and telemetry for one site.

Phase 0 runs the agent in-process with the core. In Phase 5 it becomes a separate
edge service that talks to the core over the message bus (see docs/plan/01-ARCHITECTURE.md).
"""
