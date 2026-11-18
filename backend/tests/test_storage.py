"""Unit tests for app/core/storage.py and STORAGE_PROVIDER configuration."""

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    FileTooLargeError,
    StorageError,
    UnsupportedMediaTypeError,
)
from app.core.storage import (
    MAX_PROFILE_PHOTO_BYTES,
    InMemoryStorageBackend,
    LocalStorageBackend,
    S3StorageBackend,
    get_storage_backend,
    validate_image_upload,
)
from pydantic import AnyHttpUrl, ValidationError


class _FakeChunkedFile:
    """Async readable file yielding fixed-size chunks and tracking reads."""

    def __init__(self, payload: bytes, chunk_size: int = 1024) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0
        self.bytes_read = 0

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._payload):
            return b""
        limit = self._chunk_size if size < 0 else min(size, self._chunk_size)
        chunk = self._payload[self._offset : self._offset + limit]
        self._offset += len(chunk)
        self.bytes_read += len(chunk)
        return chunk


class _FakeS3Client:
    """In-memory mock matching S3ClientProtocol."""

    def __init__(self, *, fail_put: bool = False, fail_delete: bool = False) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.fail_put = fail_put
        self.fail_delete = fail_delete
        self.deleted_keys: list[tuple[str, str]] = []

    def put_object(self, **kwargs: str | bytes) -> object:
        if self.fail_put:
            raise RuntimeError("S3 put failed")
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        content_type = str(kwargs["ContentType"])
        self.objects[(bucket, key)] = (body, content_type)
        return {"ETag": '"mock-etag"'}

    def delete_object(self, **kwargs: str) -> object:
        if self.fail_delete:
            raise RuntimeError("S3 delete failed")
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        self.deleted_keys.append((bucket, key))
        self.objects.pop((bucket, key), None)
        return {}


# ─── Settings Validation Tests ────────────────────────────────────────────────


def test_storage_settings_s3_requires_non_empty_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_S3_BUCKET", "bucket")
    with pytest.raises(ValidationError, match="AWS_ACCESS_KEY_ID"):
        Settings()

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "   ")
    with pytest.raises(ValidationError, match="AWS_SECRET_ACCESS_KEY"):
        Settings()

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_S3_BUCKET", "")
    with pytest.raises(ValidationError, match="AWS_S3_BUCKET"):
        Settings()

    monkeypatch.setenv("AWS_S3_BUCKET", "urgenthelp-media")
    cfg = Settings()
    assert cfg.STORAGE_PROVIDER == "s3"
    assert cfg.AWS_S3_BUCKET == "urgenthelp-media"


def test_storage_settings_local_and_memory_do_not_require_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_PROVIDER", "local")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "")
    monkeypatch.setenv("AWS_S3_BUCKET", "")
    local_cfg = Settings()
    assert local_cfg.STORAGE_PROVIDER == "local"

    monkeypatch.setenv("STORAGE_PROVIDER", "memory")
    mem_cfg = Settings()
    assert mem_cfg.STORAGE_PROVIDER == "memory"


def test_storage_settings_local_rejects_blank_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", "   ")
    with pytest.raises(ValidationError, match="LOCAL_STORAGE_DIR"):
        Settings()


def test_storage_settings_rejects_memory_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORAGE_PROVIDER", "memory")
    with pytest.raises(ValidationError, match="production"):
        Settings()


# ─── Image Magic-Byte & Size Validation Tests ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "expected_mime", "expected_ext"),
    [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg", "jpg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png", "png"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp", "webp"),
    ],
)
async def test_validate_image_upload_accepts_valid_magic_bytes(
    header: bytes,
    expected_mime: str,
    expected_ext: str,
) -> None:
    payload = header + b"\x00" * 64
    result = await validate_image_upload(_FakeChunkedFile(payload, chunk_size=16))
    assert result.content_type == expected_mime
    assert result.extension == expected_ext
    assert result.data == payload


@pytest.mark.asyncio
async def test_validate_image_upload_accepts_exact_max_bytes() -> None:
    payload = b"\xff\xd8\xff\xe0" + b"a" * (MAX_PROFILE_PHOTO_BYTES - 4)
    assert len(payload) == MAX_PROFILE_PHOTO_BYTES
    result = await validate_image_upload(_FakeChunkedFile(payload, chunk_size=64 * 1024))
    assert len(result.data) == MAX_PROFILE_PHOTO_BYTES
    assert result.content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_validate_image_upload_aborts_early_when_oversized() -> None:
    # 10 MiB payload — stream must abort right after crossing 5 MiB + 1 byte
    oversized_payload = b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024)
    fake_file = _FakeChunkedFile(oversized_payload, chunk_size=64 * 1024)

    with pytest.raises(FileTooLargeError) as exc_info:
        await validate_image_upload(fake_file)

    assert exc_info.value.status_code == 413
    assert exc_info.value.code == "STORAGE_FILE_TOO_LARGE"
    assert fake_file.bytes_read <= MAX_PROFILE_PHOTO_BYTES + 64 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forged_payload",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<?xml version="1.0"?><svg></svg>',
        b"<!DOCTYPE html><html><body>xss</body></html>",
        b"GIF89a\x01\x00\x01\x00",
        b"RIFF\x00\x00\x00\x00AVI ",
        b"",
        b"plain text disguised as jpg",
    ],
)
async def test_validate_image_upload_rejects_svg_html_and_unknown_magic(
    forged_payload: bytes,
) -> None:
    with pytest.raises(UnsupportedMediaTypeError) as exc_info:
        await validate_image_upload(_FakeChunkedFile(forged_payload))
    assert exc_info.value.status_code == 415
    assert exc_info.value.code == "STORAGE_UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.asyncio
async def test_validate_image_upload_rejects_non_positive_max_bytes() -> None:
    fake_file = _FakeChunkedFile(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ValueError, match="positive"):
        await validate_image_upload(fake_file, max_bytes=0)
    with pytest.raises(ValueError, match="positive"):
        await validate_image_upload(fake_file, max_bytes=-1)
    assert fake_file.bytes_read == 0


@pytest.mark.asyncio
async def test_validate_image_upload_rejects_disallowed_detected_type() -> None:
    png_payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with pytest.raises(UnsupportedMediaTypeError) as exc_info:
        await validate_image_upload(
            _FakeChunkedFile(png_payload),
            allowed_types=frozenset({"image/jpeg"}),
        )
    assert exc_info.value.status_code == 415


# ─── InMemoryStorageBackend Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_in_memory_storage_backend_crud_and_url_resolution() -> None:
    backend = InMemoryStorageBackend(base_url="https://cdn.test")
    key = f"public/profile-photos/{uuid.uuid4()}/avatar.png"

    stored_key = await backend.upload(key, b"png-bytes", "image/png")
    assert stored_key == key
    assert backend.get_object(key) == (b"png-bytes", "image/png")
    assert backend.get_public_url(key) == f"https://cdn.test/{key}"
    assert backend.get_public_url("https://legacy.example.com/a.jpg") == (
        "https://legacy.example.com/a.jpg"
    )

    await backend.delete(key)
    assert backend.get_object(key) is None
    # Deleting absent key is idempotent
    await backend.delete(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "/leading/slash.png", "../escape.png", "a/../b.png", "a\\b.png", "bad\x00key.png"],
)
async def test_storage_backends_reject_invalid_keys(bad_key: str, tmp_path: Path) -> None:
    mem = InMemoryStorageBackend()
    local = LocalStorageBackend(base_dir=tmp_path)

    with pytest.raises(StorageError):
        await mem.upload(bad_key, b"data", "image/png")
    with pytest.raises(StorageError):
        await local.upload(bad_key, b"data", "image/png")
    with pytest.raises(StorageError):
        mem.get_public_url(bad_key)


# ─── LocalStorageBackend Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_storage_backend_upload_url_and_delete(tmp_path: Path) -> None:
    backend = LocalStorageBackend(base_dir=tmp_path, base_url="/storage")
    key = f"public/profile-photos/{uuid.uuid4()}/photo.webp"

    stored_key = await backend.upload(key, b"webp-bytes", "image/webp")
    assert stored_key == key
    saved_file = tmp_path / key
    assert saved_file.exists()
    assert saved_file.read_bytes() == b"webp-bytes"
    assert backend.get_public_url(key) == f"/storage/{key}"
    assert backend.get_public_url("http://example.com/pic.jpg") == "http://example.com/pic.jpg"

    await backend.delete(key)
    assert not saved_file.exists()
    await backend.delete(key)


@pytest.mark.asyncio
async def test_local_storage_backend_maps_os_errors_to_storage_error(tmp_path: Path) -> None:
    backend = LocalStorageBackend(base_dir=tmp_path)
    with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
        with pytest.raises(StorageError) as exc_info:
            await backend.upload("public/profile-photos/u1/a.jpg", b"abc", "image/jpeg")
        assert exc_info.value.status_code == 502

    with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
        with pytest.raises(StorageError) as exc_info:
            await backend.delete("public/profile-photos/u1/a.jpg")
        assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_local_storage_backend_rejects_symlink_escape(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    outside_dir = tmp_path / "outside"
    base_dir.mkdir()
    outside_dir.mkdir()
    (base_dir / "escape").symlink_to(outside_dir)

    backend = LocalStorageBackend(base_dir=base_dir)
    with pytest.raises(StorageError):
        await backend.upload("escape/pwned.jpg", b"data", "image/jpeg")


# ─── S3StorageBackend Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_storage_backend_upload_delete_and_urls() -> None:
    fake_client = _FakeS3Client()
    backend = S3StorageBackend(
        bucket="urgenthelp-bucket",
        region="eu-west-1",
        client=fake_client,
    )
    key = "public/profile-photos/user-1/avatar.jpg"

    returned_key = await backend.upload(key, b"jpg-bytes", "image/jpeg")
    assert returned_key == key
    assert fake_client.objects[("urgenthelp-bucket", key)] == (b"jpg-bytes", "image/jpeg")
    assert (
        backend.get_public_url(key)
        == "https://urgenthelp-bucket.s3.eu-west-1.amazonaws.com/public/profile-photos/user-1/avatar.jpg"
    )
    assert backend.get_public_url("https://other.cdn/img.png") == "https://other.cdn/img.png"

    await backend.delete(key)
    assert ("urgenthelp-bucket", key) not in fake_client.objects
    assert fake_client.deleted_keys == [("urgenthelp-bucket", key)]


@pytest.mark.asyncio
async def test_s3_storage_backend_custom_endpoint_url_and_error_wrapping() -> None:
    r2_client = _FakeS3Client(fail_put=True, fail_delete=True)
    backend = S3StorageBackend(
        bucket="r2-bucket",
        region="auto",
        endpoint_url="https://acct.r2.cloudflarestorage.com/",
        client=r2_client,
    )
    assert (
        backend.get_public_url("public/profile-photos/u1/a.png")
        == "https://acct.r2.cloudflarestorage.com/r2-bucket/public/profile-photos/u1/a.png"
    )

    with pytest.raises(StorageError) as put_exc:
        await backend.upload("public/profile-photos/u1/a.png", b"data", "image/png")
    assert put_exc.value.status_code == 502

    with pytest.raises(StorageError) as del_exc:
        await backend.delete("public/profile-photos/u1/a.png")
    assert del_exc.value.status_code == 502


# ─── get_storage_backend Factory Tests ────────────────────────────────────────


def test_get_storage_backend_selects_configured_provider(tmp_path: Path) -> None:
    base = get_settings()

    mem_backend = get_storage_backend(base.model_copy(update={"STORAGE_PROVIDER": "memory"}))
    assert isinstance(mem_backend, InMemoryStorageBackend)

    local_backend = get_storage_backend(
        base.model_copy(update={"STORAGE_PROVIDER": "local", "LOCAL_STORAGE_DIR": str(tmp_path)})
    )
    assert isinstance(local_backend, LocalStorageBackend)
    assert local_backend._base_dir == tmp_path.resolve()

    s3_settings = base.model_copy(
        update={
            "STORAGE_PROVIDER": "s3",
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
            "AWS_S3_BUCKET": "test-bucket",
            "AWS_S3_REGION": "us-east-1",
            "AWS_S3_ENDPOINT_URL": AnyHttpUrl("https://r2.example.com"),
        }
    )
    with patch("app.core.storage.boto3.client", return_value=_FakeS3Client()) as mock_boto:
        s3_backend = get_storage_backend(s3_settings)
    assert isinstance(s3_backend, S3StorageBackend)
    mock_boto.assert_called_once_with(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        endpoint_url="https://r2.example.com",
    )


def test_get_storage_backend_s3_missing_or_whitespace_creds_raises_storage_error() -> None:
    base = get_settings()
    for field in ("AWS_S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        broken_none = base.model_copy(update={"STORAGE_PROVIDER": "s3", field: None})
        with pytest.raises(StorageError):
            get_storage_backend(broken_none)
        broken_ws = base.model_copy(update={"STORAGE_PROVIDER": "s3", field: "   "})
        with pytest.raises(StorageError):
            get_storage_backend(broken_ws)


def test_s3_storage_backend_wraps_boto3_init_failure() -> None:
    with patch("app.core.storage.boto3.client", side_effect=RuntimeError("bad endpoint")):
        with pytest.raises(StorageError) as exc_info:
            S3StorageBackend(bucket="b", region="us-east-1")
    assert exc_info.value.status_code == 502
