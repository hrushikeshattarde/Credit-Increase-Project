"""Tracks which request emails already have a draft or card, so re-runs are idempotent.

Two backends with the same interface: a local JSON file (workstation runs) and an
object in the stack's S3 bucket (the worker Lambda). Both are belt and braces on
top of the approvals store check, which is what really prevents duplicate cards.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ProcessedStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}

    def has(self, message_id: str) -> bool:
        return message_id in self._data

    def get(self, message_id: str) -> dict:
        return dict(self._data.get(message_id) or {})

    def mark(self, message_id: str, **info) -> None:
        self._data[message_id] = {"processed_at": datetime.now(timezone.utc).isoformat(), **info}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")


class S3ProcessedStore(ProcessedStore):
    """Same contract, persisted as one JSON object in S3 (state/processed.json)."""

    def __init__(self, bucket: str, key: str = "state/processed.json"):
        import boto3

        self.bucket, self.key = bucket, key
        self._s3 = boto3.client("s3")
        self.path = Path("/tmp") / "processed.json"
        self._data = {}
        try:
            raw = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            self._data = json.loads(raw)
        except self._s3.exceptions.NoSuchKey:
            pass
        except json.JSONDecodeError:
            pass

    def _save(self) -> None:
        self._s3.put_object(
            Bucket=self.bucket, Key=self.key,
            Body=json.dumps(self._data, indent=2).encode("utf-8"), ContentType="application/json",
        )
