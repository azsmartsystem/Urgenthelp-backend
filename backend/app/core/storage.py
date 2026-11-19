"""Object storage abstraction and bounded upload validation.

Supports AWS S3 / Cloudflare R2 (`S3StorageBackend`), local filesystem
(`LocalStorageBackend`), and in-memory (`InMemoryStorageBackend`) backends
selected via `Settings.STORAGE_PROVIDER`.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3  # type: ignore[import-untyped]  # boto3 ships without PEP 561 type stubs
import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    FileTooLargeError,
    StorageError,
    UnsupportedMediaTypeError,
)

logger = structlog.get_logger(__name__)

MAX_PROFILE_PHOTO_BYTES: int = 5 * 1024 * 1024  # 5 MiB
MAX_IDENTITY_DOCUMENT_BYTES: int = 5 * 1024 * 1024  # 5 MiB
_READ_CHUNK_SIZE: int = 64 * 1024  # 64 KiB
DEFAULT_ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})
DEFAULT_ALLOWED_DOCUMENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "application/pdf"}
)


@dataclass(frozen=True)
class ValidatedImage:
    """Result of streaming size and magic-byte validation."""

    data: bytes
    content_type: str
    extension: str


class AsyncReadableFile(Protocol):
    """Minimal async file-like protocol matching FastAPI UploadFile."""

    async def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes from the stream."""
        ...


class S3ClientProtocol(Protocol):
    """Typed subset of a boto3 S3 client used by S3StorageBackend."""

    def put_object(self, **kwargs: str | bytes) -> object:
        """Upload an object to S3."""
        ...

    def delete_object(self, **kwargs: str) -> object:
        """Delete an object from S3."""
        ...


class StorageBackend(Protocol):
    """Abstract object storage backend protocol."""

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        """Persist bytes at ``key`` and return the normalized object key."""
        ...

    async def delete(self, key: str) -> None:
        """Delete the object at ``key`` (no-op if already absent)."""
        ...

    def get_public_url(self, key: str) -> str:
        """Resolve an object key (or pass through an HTTP/HTTPS URL) to a public URL."""
        ...


def _normalize_key(key: str) -> str:
    """Validate and normalize an object key, preventing path traversal."""
    if not key or not key.strip():
        raise StorageError(detail="Storage object key cannot be empty.")
    if "\x00" in key or "\\" in key or key.startswith("/"):
        raise StorageError(detail="Invalid storage object key.")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StorageError(detail="Invalid storage object key.")
    return "/".join(parts)


def _detect_image_magic(data: bytes) -> tuple[str, str] | None:
    """Inspect magic bytes and return ``(mime_type, extension)`` or ``None``."""
    if data.startswith(b"\xff\xd8\xff"):
        return ("image/jpeg", "jpg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("image/png", "png")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image/webp", "webp")
    return None


def _detect_document_magic(data: bytes) -> tuple[str, str] | None:
    """Inspect magic bytes for identity documents (JPEG, PNG, PDF)."""
    image_match = _detect_image_magic(data)
    if image_match is not None:
        return image_match
    if data.startswith(b"%PDF-"):
        return ("application/pdf", "pdf")
    return None


async def _read_bounded_stream(file: AsyncReadableFile, max_bytes: int) -> bytes:
    """Stream-read up to ``max_bytes`` bytes, raising ``FileTooLargeError`` if exceeded."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive.")

    chunks: list[bytes] = []
    total_bytes = 0

    while True:
        remaining_plus_one = max_bytes - total_bytes + 1
        to_read = min(_READ_CHUNK_SIZE, remaining_plus_one)
        chunk = await file.read(to_read)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise FileTooLargeError(
                detail=f"Uploaded file exceeds maximum allowed size of {max_bytes} bytes.",
                context={"max_bytes": max_bytes},
            )
        chunks.append(chunk)

    return b"".join(chunks)


async def validate_image_upload(
    file: AsyncReadableFile,
    *,
    max_bytes: int = MAX_PROFILE_PHOTO_BYTES,
    allowed_types: frozenset[str] = DEFAULT_ALLOWED_IMAGE_TYPES,
) -> ValidatedImage:
    """Stream-read an uploaded image with a hard byte ceiling and verify magic bytes.

    Client-supplied ``Content-Type`` headers and filenames are never trusted;
    media type and file extension are derived solely from magic bytes.
    """
    data = await _read_bounded_stream(file, max_bytes)
    detected = _detect_image_magic(data)
    if detected is None:
        raise UnsupportedMediaTypeError(
            detail="Unsupported file type. Only JPEG, PNG, and WebP images are allowed.",
        )

    content_type, extension = detected
    if content_type not in allowed_types:
        raise UnsupportedMediaTypeError(
            detail=f"Image type '{content_type}' is not permitted for this upload.",
            context={"detected_content_type": content_type},
        )

    return ValidatedImage(
        data=data,
        content_type=content_type,
        extension=extension,
    )


async def validate_document_upload(
    file: AsyncReadableFile,
    *,
    max_bytes: int = MAX_IDENTITY_DOCUMENT_BYTES,
    allowed_types: frozenset[str] = DEFAULT_ALLOWED_DOCUMENT_TYPES,
) -> ValidatedImage:
    """Stream-read an uploaded identity document (JPEG, PNG, PDF) and verify magic bytes."""
    data = await _read_bounded_stream(file, max_bytes)
    detected = _detect_document_magic(data)
    if detected is None:
        raise UnsupportedMediaTypeError(
            detail="Unsupported file type. Only JPEG, PNG, and PDF documents are allowed.",
        )

    content_type, extension = detected
    if content_type not in allowed_types:
        raise UnsupportedMediaTypeError(
            detail=f"Document type '{content_type}' is not permitted for this upload.",
            context={"detected_content_type": content_type},
        )

    return ValidatedImage(
        data=data,
        content_type=content_type,
        extension=extension,
    )


class S3StorageBackend:
    """S3-compatible object storage backend (AWS S3 / Cloudflare R2 / MinIO)."""

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        endpoint_url: str | None = None,
        client: S3ClientProtocol | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url.rstrip("/") if endpoint_url else None
        if client is not None:
            self._client: S3ClientProtocol = client
        else:
            try:
                self._client = boto3.client(
                    "s3",
                    region_name=region,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    endpoint_url=self._endpoint_url,
                )
            except Exception as exc:
                logger.error("s3_client_init_failed", bucket=bucket, region=region, error=str(exc))
                raise StorageError(
                    detail="Failed to initialize S3 storage client.",
                    context={"bucket": bucket, "region": region},
                ) from exc

    def _put_sync(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    def _delete_sync(self, key: str) -> None:
        self._client.delete_object(
            Bucket=self._bucket,
            Key=key,
        )

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        normalized = _normalize_key(key)
        try:
            await asyncio.to_thread(self._put_sync, normalized, data, content_type)
        except Exception as exc:
            logger.error("s3_upload_failed", key=normalized, error=str(exc))
            raise StorageError(
                detail="Failed to upload file to object storage.",
                context={"key": normalized},
            ) from exc
        logger.info("s3_object_uploaded", key=normalized, content_type=content_type, size=len(data))
        return normalized

    async def delete(self, key: str) -> None:
        normalized = _normalize_key(key)
        try:
            await asyncio.to_thread(self._delete_sync, normalized)
        except Exception as exc:
            logger.error("s3_delete_failed", key=normalized, error=str(exc))
            raise StorageError(
                detail="Failed to delete file from object storage.",
                context={"key": normalized},
            ) from exc
        logger.info("s3_object_deleted", key=normalized)

    def get_public_url(self, key: str) -> str:
        if key.startswith(("https://", "http://")):
            return key
        normalized = _normalize_key(key)
        if self._endpoint_url is not None:
            return f"{self._endpoint_url}/{self._bucket}/{normalized}"
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{normalized}"


class LocalStorageBackend:
    """Local filesystem object storage backend for offline development."""

    def __init__(self, base_dir: str | Path = "./storage", base_url: str = "/storage") -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_url = base_url.rstrip("/") or "/storage"

    def _resolve_path(self, normalized_key: str) -> Path:
        target = (self._base_dir / normalized_key).resolve()
        if not target.is_relative_to(self._base_dir):
            raise StorageError(detail="Invalid storage object key.")
        return target

    def _write_sync(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _delete_sync(self, target: Path) -> None:
        target.unlink(missing_ok=True)

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        normalized = _normalize_key(key)
        target = self._resolve_path(normalized)
        try:
            await asyncio.to_thread(self._write_sync, target, data)
        except OSError as exc:
            logger.error("local_storage_upload_failed", key=normalized, error=str(exc))
            raise StorageError(
                detail="Failed to write file to local storage.",
                context={"key": normalized},
            ) from exc
        logger.info(
            "local_object_uploaded",
            key=normalized,
            content_type=content_type,
            size=len(data),
        )
        return normalized

    async def delete(self, key: str) -> None:
        normalized = _normalize_key(key)
        target = self._resolve_path(normalized)
        try:
            await asyncio.to_thread(self._delete_sync, target)
        except OSError as exc:
            logger.error("local_storage_delete_failed", key=normalized, error=str(exc))
            raise StorageError(
                detail="Failed to delete file from local storage.",
                context={"key": normalized},
            ) from exc
        logger.info("local_object_deleted", key=normalized)

    def get_public_url(self, key: str) -> str:
        if key.startswith(("https://", "http://")):
            return key
        normalized = _normalize_key(key)
        return f"{self._base_url}/{normalized}"


class InMemoryStorageBackend:
    """In-memory object storage backend for deterministic offline tests."""

    def __init__(self, base_url: str = "https://storage.test") -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._base_url = base_url.rstrip("/")

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        normalized = _normalize_key(key)
        self._objects[normalized] = (data, content_type)
        return normalized

    async def delete(self, key: str) -> None:
        normalized = _normalize_key(key)
        self._objects.pop(normalized, None)

    def get_public_url(self, key: str) -> str:
        if key.startswith(("https://", "http://")):
            return key
        normalized = _normalize_key(key)
        return f"{self._base_url}/{normalized}"

    def get_object(self, key: str) -> tuple[bytes, str] | None:
        """Return stored ``(bytes, content_type)`` for ``key``, or ``None``."""
        normalized = _normalize_key(key)
        return self._objects.get(normalized)


def get_storage_backend(settings: Settings | None = None) -> StorageBackend:
    """Factory to instantiate the configured StorageBackend according to settings."""
    app_settings = settings or get_settings()

    if app_settings.STORAGE_PROVIDER == "memory":
        return InMemoryStorageBackend()

    if app_settings.STORAGE_PROVIDER == "local":
        return LocalStorageBackend(base_dir=app_settings.LOCAL_STORAGE_DIR)

    bucket = (app_settings.AWS_S3_BUCKET or "").strip()
    access_key = (app_settings.AWS_ACCESS_KEY_ID or "").strip()
    secret_key = (app_settings.AWS_SECRET_ACCESS_KEY or "").strip()
    if not bucket or not access_key or not secret_key:
        raise StorageError(
            detail="S3 storage is selected but AWS credentials or bucket are not configured."
        )

    endpoint_url = (
        str(app_settings.AWS_S3_ENDPOINT_URL)
        if app_settings.AWS_S3_ENDPOINT_URL is not None
        else None
    )
    return S3StorageBackend(
        bucket=bucket,
        region=app_settings.AWS_S3_REGION,
        access_key_id=access_key,
        secret_access_key=secret_key,
        endpoint_url=endpoint_url,
    )
