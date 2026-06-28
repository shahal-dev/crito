"""Device Abstraction Layer (DAL).

Roles (Mount, Camera, ...) are vendor-neutral interfaces. Adapters implement them
against a concrete ecosystem. Phase 0 ships the INDI adapter; the Alpaca adapter is
added later behind the same roles (see docs/plan/02-DEVICE-CONTROL.md).
"""
