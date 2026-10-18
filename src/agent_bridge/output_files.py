"""Local, event-backed media delivery. No discovery scans imply success."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import struct
import uuid
from pathlib import Path
from typing import BinaryIO

from agent_bridge.models import OutputFile

MAX_MEDIA_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_FILES = 16
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}


def checked_path(path: Path, root: Path, *, regular: bool = True) -> Path:
    """Reject traversal, links and Windows junctions, including linked parents."""
    if ".." in path.parts or ".." in root.parts:
        raise ValueError("media path traversal rejected")
    absolute = path.absolute()
    boundary = root.absolute()
    if not absolute.is_relative_to(boundary):
        raise ValueError("media path is outside the allowed directory")
    for part in [absolute, *absolute.parents]:
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("media links and reparse points are not allowed")
    if not absolute.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("media path escaped its directory")
    if regular and not stat.S_ISREG(absolute.stat().st_mode):
        raise ValueError("media path is not a regular file")
    return absolute


def existing_files(root: Path) -> set[Path]:
    """A deny-set of pre-turn names; never use it to discover deliverables."""
    checked_path(root, root, regular=False)
    if not root.exists():
        return set()
    found: set[Path] = set()
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories
            if not Path(folder, name).is_symlink()
            and not getattr(Path(folder, name).lstat(), "st_file_attributes", 0) & 0x400
        ]
        found.update(Path(folder, name).absolute() for name in files)
        if len(found) > 4096:
            raise ValueError("native media inventory limit exceeded")
    return found


def _mime(handle: BinaryIO, size: int) -> str:
    handle.seek(0)
    head = handle.read(64)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(head) < 33 or head[8:16] != b"\x00\x00\x00\rIHDR":
            raise ValueError("invalid PNG header")
        width, height = struct.unpack(">II", head[16:24])
        handle.seek(-12, os.SEEK_END)
        if not width or not height or handle.read(12) != b"\x00\x00\x00\x00IEND\xaeB`\x82":
            raise ValueError("invalid PNG dimensions or end marker")
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        handle.seek(-2, os.SEEK_END)
        if size < 16 or handle.read(2) != b"\xff\xd9":
            raise ValueError("invalid JPEG end marker")
        return "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        if size < 20 or struct.unpack("<I", head[4:8])[0] + 8 != size or head[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}:
            raise ValueError("invalid WebP container")
        return "image/webp"
    if head[4:8] == b"ftyp":
        offset = 0
        boxes: set[bytes] = set()
        while offset < size:
            handle.seek(offset)
            header = handle.read(16)
            if len(header) < 8:
                raise ValueError("truncated MP4 box")
            length, kind = struct.unpack(">I4s", header[:8])
            if length == 1:
                if len(header) < 16:
                    raise ValueError("truncated MP4 extended box")
                length = struct.unpack(">Q", header[8:16])[0]
                minimum = 16
            else:
                minimum = 8
            if length == 0:
                length = size - offset
            if length < minimum or length > size - offset:
                raise ValueError("invalid MP4 box length")
            boxes.add(kind)
            offset += length
        if not {b"ftyp", b"moov", b"mdat"} <= boxes:
            raise ValueError("MP4 lacks movie or media data")
        return "video/mp4"
    raise ValueError("unsupported or forged media type")


def inspect_media(path: Path, *, max_bytes: int = MAX_MEDIA_BYTES) -> tuple[str, int, str]:
    """Read MIME, size, and digest of one regular file without copying it."""
    with path.open("rb") as handle:
        return _inspect(handle, max_bytes)


def _inspect(handle: BinaryIO, max_bytes: int) -> tuple[str, int, str]:
    before = os.fstat(handle.fileno())
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
        raise ValueError("media is empty, oversized or not a regular file")
    mime = _mime(handle, before.st_size)
    handle.seek(0)
    digest = hashlib.sha256()
    total = 0
    while chunk := handle.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("media exceeded size limit")
        digest.update(chunk)
    after = os.fstat(handle.fileno())
    if total != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("media changed during validation")
    return mime, total, digest.hexdigest()


def deliver_file(
    source: Path,
    *,
    allowed_root: Path,
    cwd: Path,
    task_id: str,
    tool_call_id: str,
    previous: set[Path],
    expected: str,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> OutputFile:
    if not task_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in task_id):
        raise ValueError("invalid task artifact identifier")
    source = checked_path(source, allowed_root)
    if source in previous:
        raise ValueError("pre-turn media cannot be delivered")
    # O_NOFOLLOW closes the final-component race on platforms that support it.
    identity = source.stat()
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as input_handle:
        opened = os.fstat(input_handle.fileno())
        if (identity.st_dev, identity.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("media path changed before it could be opened")
        checked_path(source, allowed_root)
        mime, count, sha256 = _inspect(input_handle, max_bytes)
        if not mime.startswith(expected + "/"):
            raise ValueError("media type does not match the native tool")
        folder = cwd.absolute() / ".agent-bridge" / "outputs" / task_id
        checked_path(folder, cwd, regular=False)
        folder.mkdir(parents=True, exist_ok=True)
        checked_path(folder, cwd, regular=False)
        output = folder / (uuid.uuid4().hex + _EXTENSIONS[mime])
        created = False
        try:
            with output.open("x+b") as output_handle:
                created = True
                input_handle.seek(0)
                copied = 0
                while chunk := input_handle.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise ValueError("media exceeded copy limit")
                    output_handle.write(chunk)
                output_handle.flush()
                saved_mime, saved_count, saved_hash = _inspect(output_handle, max_bytes)
                if (saved_mime, saved_count, saved_hash) != (mime, count, sha256):
                    raise ValueError("media changed during copy")
        except BaseException:
            if created:
                with contextlib.suppress(OSError):
                    output.unlink()
            raise
    return OutputFile(
        path=output.relative_to(cwd.absolute()).as_posix(), mime=mime,
        bytes=count, sha256=sha256, tool_call_id=tool_call_id,
    )


def verify_output(output: OutputFile, cwd: Path, task_id: str) -> None:
    root = cwd.absolute() / ".agent-bridge" / "outputs" / task_id
    path = checked_path(cwd / output.path, root)
    mime, count, sha256 = inspect_media(path)
    if (mime, count, sha256) != (output.mime, output.bytes, output.sha256):
        raise ValueError("delivered media no longer matches recorded metadata")
