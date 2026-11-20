"""Object storage through the S3 API (SeaweedFS locally, R2/S3 later: config change only).

boto3 is blocking; from async code call these methods via asyncio.to_thread.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

from clipper_core.config import Settings
from clipper_core.logs import get_logger

log = get_logger(__name__)


class keys:
    """The storage layout. Every service builds keys only through these functions."""

    @staticmethod
    def chunk_video(stream_id: int, seq: int) -> str:
        return f"streams/{stream_id}/video/{seq:06d}.ts"

    @staticmethod
    def chunk_audio(stream_id: int, seq: int) -> str:
        return f"streams/{stream_id}/audio/{seq:06d}.aac"

    @staticmethod
    def chunk_words(stream_id: int, seq: int) -> str:
        return f"streams/{stream_id}/transcripts/{seq:06d}.json"

    @staticmethod
    def judge_io(request_hash: str, clip_id: int | None = None) -> str:
        """Request + raw response of a judge call (`judge_calls.io_key`; evals, prompt work).

        Keyed on the call's `request_hash` (hex sha256), so the judge uploads it before the
        `judge_calls` row exists; calls outside a clip (evals) go under `judge/`. A repeated
        request (a retry after an error) overwrites the earlier object."""
        if not re.fullmatch(r"[0-9a-f]+", request_hash):
            raise ValueError(f"request hash must be lowercase hex: {request_hash!r}")
        prefix = "judge" if clip_id is None else f"clips/{clip_id}/judge"
        return f"{prefix}/{request_hash}.json"


class ObjectStore:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def ensure_bucket(self, wait_s: float = 60.0) -> None:
        """Create the bucket if missing; waits for the storage server to come up."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self._client.head_bucket(Bucket=self.bucket)
                return
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket", "NotFound"):
                    self._client.create_bucket(Bucket=self.bucket)
                    log.info("created bucket", bucket=self.bucket)
                    return
                if time.monotonic() > deadline:
                    raise
            except EndpointConnectionError:
                if time.monotonic() > deadline:
                    raise
            log.info("waiting for object storage", endpoint=self._client.meta.endpoint_url)
            time.sleep(2)

    def put_file(self, key: str, path: Path | str, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else None
        self._client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        kwargs = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **kwargs)

    def get_bytes(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def download_file(self, key: str, path: Path | str) -> None:
        self._client.download_file(self.bucket, key, str(path))

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def presigned_url(self, key: str, expires_s: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_s
        )
