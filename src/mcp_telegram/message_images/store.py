"""Private bounded JPEG artifact publication outside database transactions."""

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from .limits import PHOTO_MAX_BYTES

_ARTIFACT_STEM_LENGTH = 32


@dataclass(frozen=True, slots=True)
class StoredImage:
    filename: str
    sha256: str
    byte_size: int


def cleanup_unpublished(directory: Path, account_id: int, referenced: frozenset[str]) -> None:
    """Remove only owned partials and unassociated artifacts before worker startup."""
    account_dir = directory / str(account_id)
    if directory.is_symlink() or not account_dir.is_dir() or account_dir.is_symlink():
        return
    for path in account_dir.iterdir():
        if _is_unpublished_artifact(path, referenced):
            path.unlink(missing_ok=True)


def _is_unpublished_artifact(path: Path, referenced: frozenset[str]) -> bool:
    stem = path.stem
    return (
        len(stem) == _ARTIFACT_STEM_LENGTH
        and all(c in "0123456789abcdef" for c in stem)
        and path.suffix in {".jpg", ".partial"}
        and path.name not in referenced
        and not path.is_symlink()
        and path.is_file()
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_account_directory(directory: Path, account_id: int) -> Path:
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise OSError("invalid image storage directory")
    directory.chmod(0o700)
    _fsync_directory(directory.parent)
    account_dir = directory / str(account_id)
    account_dir.mkdir(exist_ok=True, mode=0o700)
    if account_dir.is_symlink():
        raise OSError("invalid image storage directory")
    account_dir.chmod(0o700)
    _fsync_directory(directory)
    return account_dir


def publish_image(directory: Path, account_id: int, image: bytes) -> StoredImage:
    """Durably write one opaque server-named JPEG and return private metadata."""
    if (
        not 0 < len(image) <= PHOTO_MAX_BYTES
        or not image.startswith(b"\xff\xd8\xff")
        or not image.endswith(b"\xff\xd9")
    ):
        raise ValueError("invalid JPEG image")
    account_dir = _prepare_account_directory(directory, account_id)
    name = secrets.token_hex(16)
    partial = account_dir / (name + ".partial")
    final = account_dir / (name + ".jpg")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(image)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(final)
        _fsync_directory(account_dir)
    except BaseException:
        partial.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise
    return StoredImage(final.name, hashlib.sha256(image).hexdigest(), len(image))
