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


class LocalDocs:
    """Named JSON documents in a folder (workstation runs of the PITT report)."""

    def __init__(self, folder: Path):
        self.folder = folder

    def _path(self, name: str) -> Path:
        return self.folder / name

    def get(self, name: str) -> dict | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, name: str, data: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        tmp.replace(self._path(name))

    def create(self, name: str, data: dict) -> bool:
        """Create the document only if it does not exist yet (a claim)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path(name), "x", encoding="utf-8") as fh:
                json.dump(data, fh, default=str)
            return True
        except FileExistsError:
            return False

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


class S3Docs(LocalDocs):
    """Same contract as objects under <prefix> in the state bucket (the worker Lambda)."""

    def __init__(self, bucket: str, prefix: str = "state/"):
        import boto3

        self.bucket, self.prefix = bucket, prefix
        self._s3 = boto3.client("s3")

    def get(self, name: str) -> dict | None:
        try:
            raw = self._s3.get_object(Bucket=self.bucket, Key=self.prefix + name)["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, name: str, data: dict) -> None:
        self._s3.put_object(
            Bucket=self.bucket, Key=self.prefix + name,
            Body=json.dumps(data, default=str).encode("utf-8"), ContentType="application/json",
        )

    def create(self, name: str, data: dict) -> bool:
        try:
            self._s3.put_object(
                Bucket=self.bucket, Key=self.prefix + name,
                Body=json.dumps(data, default=str).encode("utf-8"), ContentType="application/json",
                IfNoneMatch="*",  # atomic: exactly one caller creates the object
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412") or "PreconditionFailed" in str(exc):
                return False
            raise

    def delete(self, name: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self.prefix + name)
