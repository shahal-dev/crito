"""Object storage abstraction.

Phase 1 ships a local-filesystem store; an S3/MinIO store implements the same tiny
interface later (see docs/plan/03-DATA-PIPELINE.md / 09-TECH-STACK.md) without
touching the archive logic.
"""
from __future__ import annotations

import pathlib


class LocalStore:
    def __init__(self, root: str):
        self.root = pathlib.Path(root)

    def put(self, key: str, data: bytes) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()
