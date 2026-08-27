"""Upload episodes, artwork and the feed to Cloudflare R2 (S3-compatible)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import StorageConfig

log = logging.getLogger(__name__)


class R2Storage:
    def __init__(self, config: StorageConfig, *, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.endpoint_url,
                aws_access_key_id=self.config.access_key_id,
                aws_secret_access_key=self.config.secret_access_key,
                region_name="auto",
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 5, "mode": "standard"},
                ),
            )
        return self._client

    # ------------------------------------------------------------- objects --

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def upload_file(self, path: Path, key: str, content_type: str) -> str:
        url = self.config.public_url(key)
        if self.dry_run:
            log.info("[dry-run] would upload %s -> %s", path.name, key)
            return url
        log.info("uploading %s (%s bytes) -> %s", path.name, path.stat().st_size, key)
        with open(path, "rb") as handle:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=handle,
                ContentType=content_type,
                CacheControl=self.config.media_cache_control,
            )
        return url

    def upload_bytes(
        self, data: bytes, key: str, content_type: str, cache_control: str | None = None
    ) -> str:
        url = self.config.public_url(key)
        if self.dry_run:
            log.info("[dry-run] would upload %d bytes -> %s", len(data), key)
            return url
        log.info("uploading %d bytes -> %s", len(data), key)
        self.client.put_object(
            Bucket=self.config.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=cache_control or self.config.media_cache_control,
        )
        return url

    def get_json(self, key: str) -> dict | None:
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        try:
            return json.loads(response["Body"].read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("ignoring unreadable %s: %s", key, exc)
            return None

    def put_json(self, key: str, payload: dict) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.upload_bytes(
            data, key, "application/json", cache_control="no-cache, max-age=0"
        )
