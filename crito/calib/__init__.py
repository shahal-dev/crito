"""Sensor characterization + exposure planning support.

``analysis`` is pure math over calibration frames (no hardware) and is unit-tested
offline. ``characterize`` drives the real camera over INDI to take those frames and
writes a calibration table that ``crito.transient.exposure`` consumes.
"""
