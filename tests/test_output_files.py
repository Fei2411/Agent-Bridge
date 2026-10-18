"""Offline boundaries for local media delivery.

Observable acceptance and rejection only: hashes come from the delivered
bytes, and private parsers are not used as an oracle.
"""

from __future__ import annotations

import errno
import hashlib
import os
import struct
import zlib
from pathlib import Path

import pytest

from agent_bridge.output_files import checked_path, deliver_file, existing_files, verify_output

_SYMLINK_WINERRORS = {5, 1314}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_bytes(color: bytes = b"\xff\x00\x00") -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" + color))
        + _png_chunk(b"IEND", b"")
    )


def jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + b"\x00" * 8 + b"\xff\xd9"


def webp_bytes() -> bytes:
    chunk = b"VP8 " + struct.pack("<I", 4) + b"data"
    payload = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mp4_bytes() -> bytes:
    return _box(b"ftyp", b"isom") + _box(b"moov") + _box(b"mdat", b"\x00\x00")


def _zero_width_png() -> bytes:
    raw = bytearray(png_bytes())
    raw[16:20] = b"\x00\x00\x00\x00"
    return bytes(raw)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    allowed = tmp_path / "allowed"
    cwd = tmp_path / "cwd"
    allowed.mkdir()
    cwd.mkdir()
    return allowed, cwd


def _deliver(
    source: Path,
    allowed: Path,
    cwd: Path,
    previous: set[Path],
    *,
    task_id: str = "task_a",
    expected: str = "image",
    tool_call_id: str | None = "call-1",
    max_bytes: int | None = None,
):
    extra = {} if max_bytes is None else {"max_bytes": max_bytes}
    return deliver_file(
        source,
        allowed_root=allowed,
        cwd=cwd,
        task_id=task_id,
        tool_call_id=tool_call_id,
        previous=previous,
        expected=expected,
        **extra,
    )


def _published_files(cwd: Path) -> list[Path]:
    root = cwd / ".agent-bridge"
    if not root.exists() or root.is_symlink():
        return [root] if root.is_symlink() else []
    found: list[Path] = []
    for folder, directories, files in os.walk(root, followlinks=False):
        found.extend(Path(folder, name) for name in files)
        found.extend(path for name in directories if (path := Path(folder, name)).is_symlink())
        directories[:] = [name for name in directories if not Path(folder, name).is_symlink()]
    return sorted(found)


def _files_nofollow(root: Path) -> list[Path]:
    if not root.exists() or root.is_symlink():
        return []
    found: list[Path] = []
    for folder, directories, files in os.walk(root, followlinks=False):
        found.extend(Path(folder, name) for name in files)
        directories[:] = [name for name in directories if not Path(folder, name).is_symlink()]
    return sorted(found)


def _symlink(target: Path, link: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        winerror = getattr(exc, "winerror", None)
        if isinstance(exc, NotImplementedError) or winerror in _SYMLINK_WINERRORS or exc.errno in {
            errno.EPERM,
            errno.EACCES,
            errno.ENOTSUP,
        }:
            pytest.skip(f"symlink creation is not permitted: {exc}")
        raise
    if not link.is_symlink():
        pytest.skip("platform did not create a symlink")


def test_checked_path_accepts_a_regular_file_inside_its_root(tmp_path: Path):
    allowed, _cwd = _workspace(tmp_path)
    source = allowed / "note.txt"
    source.write_bytes(b"hello")

    assert checked_path(source, allowed) == source.absolute()
    assert checked_path(allowed, allowed, regular=False) == allowed.absolute()


def test_directory_source_is_rejected_and_not_published(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    nested = allowed / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        checked_path(nested, allowed)
    with pytest.raises(ValueError, match="not a regular file"):
        _deliver(nested, allowed, cwd, set())

    assert _published_files(cwd) == []


def test_lexical_traversal_is_rejected_before_normalization(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    nested = allowed / "nested"
    nested.mkdir()
    source = allowed / "pic.png"
    payload = png_bytes()
    source.write_bytes(payload)
    via_parent = nested / ".." / "pic.png"
    assert via_parent.resolve() == source.resolve()

    with pytest.raises(ValueError, match="traversal"):
        checked_path(via_parent, allowed)
    with pytest.raises(ValueError, match="traversal"):
        checked_path(source, nested / "..")
    with pytest.raises(ValueError, match="traversal"):
        _deliver(via_parent, allowed, cwd, set())
    with pytest.raises(ValueError, match="traversal"):
        existing_files(nested / "..")

    assert source.read_bytes() == payload
    assert _published_files(cwd) == []


def test_sibling_prefix_and_absolute_paths_stay_outside(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    sibling = tmp_path / "allowed-extra"
    sibling.mkdir()
    source = sibling / "pic.png"
    payload = png_bytes()
    source.write_bytes(payload)
    missing = Path("missing-media.bin")

    with pytest.raises(ValueError, match="outside"):
        checked_path(source, allowed)
    with pytest.raises(ValueError, match="outside"):
        checked_path(missing, allowed)
    with pytest.raises(ValueError, match="outside"):
        _deliver(source, allowed, cwd, set())

    assert source.read_bytes() == payload
    assert _published_files(cwd) == []


def test_missing_source_is_not_published(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)

    with pytest.raises(FileNotFoundError):
        _deliver(allowed / "missing.png", allowed, cwd, set())

    assert _published_files(cwd) == []


def test_png_delivery_records_digest_and_workspace_relative_path(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)

    output = _deliver(
        source,
        allowed,
        cwd,
        set(),
        task_id="A-1_b",
        tool_call_id="../not-a-path",
        max_bytes=len(payload),
    )
    delivered = cwd / output.path

    assert output.mime == "image/png"
    assert output.bytes == len(payload)
    assert output.sha256 == hashlib.sha256(payload).hexdigest()
    assert output.tool_call_id == "../not-a-path"
    assert delivered.read_bytes() == payload
    assert output.path == delivered.relative_to(cwd).as_posix()
    assert output.path.startswith(".agent-bridge/outputs/A-1_b/")
    assert output.path.endswith(".png")
    assert "\\" not in output.path
    assert ".." not in Path(output.path).parts
    assert delivered.is_file()
    assert not delivered.is_symlink()
    verify_output(output, cwd, "A-1_b")


@pytest.mark.parametrize(
    ("payload", "mime", "expected", "suffix"),
    [
        (jpeg_bytes(), "image/jpeg", "image", ".jpg"),
        (webp_bytes(), "image/webp", "image", ".webp"),
        (mp4_bytes(), "video/mp4", "video", ".mp4"),
    ],
    ids=["jpeg", "webp", "mp4"],
)
def test_structural_media_is_named_from_content(tmp_path: Path, payload: bytes, mime: str, expected: str, suffix: str):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "labeled.png"
    source.write_bytes(payload)

    output = _deliver(source, allowed, cwd, set(), expected=expected)
    delivered = cwd / output.path

    assert output.mime == mime
    assert output.bytes == len(payload)
    assert output.sha256 == hashlib.sha256(delivered.read_bytes()).hexdigest()
    assert output.path.endswith(suffix)
    assert not output.path.endswith(".png")
    verify_output(output, cwd, "task_a")


def test_rejects_empty_and_oversize_without_publishing(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    empty = allowed / "empty.png"
    empty.write_bytes(b"")
    payload = png_bytes()
    image = allowed / "exact.png"
    image.write_bytes(payload)

    with pytest.raises(ValueError, match="empty, oversized"):
        _deliver(empty, allowed, cwd, set())
    with pytest.raises(ValueError, match="empty, oversized"):
        _deliver(image, allowed, cwd, set(), max_bytes=len(payload) - 1)

    assert image.read_bytes() == payload
    assert _published_files(cwd) == []


@pytest.mark.parametrize(
    ("payload", "expected", "message"),
    [
        (b"not-an-image", "image", "unsupported or forged"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image", "invalid PNG header"),
        (png_bytes()[:-1], "image", "invalid PNG dimensions or end marker"),
        (_zero_width_png(), "image", "invalid PNG dimensions or end marker"),
        (b"\xff\xd8\xff" + b"\x00" * 16, "image", "invalid JPEG end marker"),
        (b"RIFF" + struct.pack("<I", 999) + b"WEBP" + b"VP8 " + b"\x00" * 8, "image", "invalid WebP container"),
        (_box(b"ftyp", b"isom"), "video", "MP4 lacks movie or media data"),
    ],
    ids=["text", "bad-png", "truncated-png", "zero-width-png", "bad-jpeg", "bad-webp", "ftyp-only"],
)
def test_forged_media_is_rejected(tmp_path: Path, payload: bytes, expected: str, message: str):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "forged.bin"
    source.write_bytes(payload)

    with pytest.raises(ValueError, match=message):
        _deliver(source, allowed, cwd, set(), expected=expected)

    assert _published_files(cwd) == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(png_bytes(), "video"), (mp4_bytes(), "image")],
    ids=["png-as-video", "mp4-as-image"],
)
def test_expected_media_family_mismatch_is_rejected(tmp_path: Path, payload: bytes, expected: str):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.bin"
    source.write_bytes(payload)

    with pytest.raises(ValueError, match="does not match"):
        _deliver(source, allowed, cwd, set(), expected=expected)

    assert _published_files(cwd) == []


@pytest.mark.parametrize("task_id", ["", ".", "..", "a/b", "a\\b", "a b", "a.b", "task/../x", "\u4f60\u597d"])
def test_invalid_task_id_is_rejected_before_publishing(tmp_path: Path, task_id: str):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    source.write_bytes(png_bytes())

    with pytest.raises(ValueError, match="invalid task"):
        _deliver(source, allowed, cwd, set(), task_id=task_id)

    assert _published_files(cwd) == []


def test_existing_files_records_nested_names_and_missing_root_is_empty(tmp_path: Path):
    allowed, _cwd = _workspace(tmp_path)
    nested = allowed / "nested"
    nested.mkdir()
    keep = nested / "keep.txt"
    other = allowed / "other.txt"
    keep.write_bytes(b"keep")
    other.write_bytes(b"other")

    found = existing_files(allowed)

    assert found == {keep.absolute(), other.absolute()}
    assert all(path.is_absolute() and path.is_file() for path in found)
    assert existing_files(tmp_path / "missing-root") == set()
    assert existing_files(allowed / "unused") == set()


def test_existing_files_does_not_follow_directory_links(tmp_path: Path):
    allowed, _cwd = _workspace(tmp_path)
    real = allowed / "real"
    real.mkdir()
    inside = real / "keep.png"
    inside.write_bytes(png_bytes())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(png_bytes(b"\x10\x20\x30"))
    _symlink(outside, allowed / "jump", directory=True)

    found = existing_files(allowed)

    assert inside.absolute() in found
    assert (allowed / "jump" / "secret.png").absolute() not in found
    assert all(path.is_relative_to(allowed.absolute()) for path in found)


def test_source_symlink_is_rejected(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    target = allowed / "real.png"
    payload = png_bytes()
    target.write_bytes(payload)
    link = allowed / "alias.png"
    _symlink(target, link, directory=False)
    dangling = allowed / "dangling.png"
    _symlink(allowed / "missing.png", dangling, directory=False)

    with pytest.raises(ValueError, match=r"links and reparse|escaped"):
        checked_path(link, allowed)
    with pytest.raises(ValueError, match=r"links and reparse|escaped"):
        _deliver(link, allowed, cwd, set())
    with pytest.raises(ValueError, match=r"links and reparse|escaped"):
        _deliver(dangling, allowed, cwd, set())

    assert target.read_bytes() == payload
    assert _published_files(cwd) == []


def test_parent_symlink_is_rejected(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    real = allowed / "real"
    real.mkdir()
    source = real / "pic.png"
    payload = png_bytes()
    source.write_bytes(payload)
    linked = allowed / "linked"
    _symlink(real, linked, directory=True)
    through_link = linked / "pic.png"

    with pytest.raises(ValueError, match=r"links and reparse|escaped"):
        checked_path(through_link, allowed)
    with pytest.raises(ValueError, match=r"links and reparse|escaped"):
        _deliver(through_link, allowed, cwd, set())

    assert source.read_bytes() == payload
    assert _published_files(cwd) == []


def test_destination_parent_symlink_receives_nothing(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    bridge = cwd / ".agent-bridge"
    bridge.mkdir()
    _symlink(outside, bridge / "outputs", directory=True)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)
    before = _files_nofollow(outside)

    with pytest.raises(ValueError, match=r"links and reparse|escaped"):
        _deliver(source, allowed, cwd, set())

    assert _files_nofollow(outside) == before
    assert source.read_bytes() == payload


def test_preturn_file_is_rejected_after_its_name_is_overwritten(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    old = allowed / "old.png"
    old.write_bytes(png_bytes(b"\xff\x00\x00"))
    previous = existing_files(allowed)
    updated = png_bytes(b"\x00\xff\x00")
    old.write_bytes(updated)
    fresh = allowed / "fresh.png"
    fresh_payload = png_bytes(b"\x00\x00\xff")
    fresh.write_bytes(fresh_payload)

    with pytest.raises(ValueError, match="pre-turn"):
        _deliver(old, allowed, cwd, previous)
    output = _deliver(fresh, allowed, cwd, previous)

    assert old.read_bytes() == updated
    assert _published_files(cwd) == [cwd / output.path]
    assert (cwd / output.path).read_bytes() == fresh_payload
    verify_output(output, cwd, "task_a")


def test_repeated_delivery_uses_distinct_names_without_overwrite(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    previous = existing_files(allowed)
    source.write_bytes(payload)

    first = _deliver(source, allowed, cwd, previous, tool_call_id="call-1")
    second = _deliver(source, allowed, cwd, previous, tool_call_id=None)
    first_path = cwd / first.path
    second_path = cwd / second.path

    assert first.path != second.path
    assert first_path.read_bytes() == payload
    assert second_path.read_bytes() == payload
    assert second.tool_call_id is None
    assert _published_files(cwd) == sorted([first_path, second_path])


def test_forced_name_collision_preserves_existing_delivery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)
    issued: list[str] = []

    def fake_uuid4():
        # Pin the generator so the no-overwrite contract does not depend on a random collision.
        if len(issued) >= 4:
            raise AssertionError("name generator did not stop retrying")
        issued.append("c" * 32)

        class _HexId:
            hex = "c" * 32

        return _HexId()

    monkeypatch.setattr("agent_bridge.output_files.uuid.uuid4", fake_uuid4)
    first = _deliver(source, allowed, cwd, set())
    destination = cwd / first.path
    stamped = destination.read_bytes() + b"STAMP"
    destination.write_bytes(stamped)

    try:
        second = _deliver(source, allowed, cwd, set())
    except (OSError, ValueError):
        second = None

    assert destination.is_file(), "name collision removed the existing delivery"
    assert destination.read_bytes() == stamped, "name collision overwrote the existing delivery"
    if second is not None:
        assert second.path != first.path
        assert (cwd / second.path).read_bytes() == payload


def test_verify_detects_metadata_and_content_corruption_without_deleting(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)
    output = _deliver(source, allowed, cwd, set())
    delivered = cwd / output.path

    verify_output(output, cwd, "task_a")
    with pytest.raises(ValueError, match="no longer matches"):
        verify_output(output.model_copy(update={"sha256": "0" * 64}), cwd, "task_a")
    with pytest.raises(ValueError, match="no longer matches"):
        verify_output(output.model_copy(update={"mime": "image/jpeg"}), cwd, "task_a")
    assert delivered.read_bytes() == payload

    flipped = bytearray(payload)
    flipped[30] ^= 0x01
    delivered.write_bytes(flipped)
    with pytest.raises(ValueError, match="no longer matches"):
        verify_output(output, cwd, "task_a")
    assert delivered.read_bytes() == flipped

    delivered.write_bytes(payload[:8])
    with pytest.raises(ValueError):
        verify_output(output, cwd, "task_a")
    assert delivered.read_bytes() == payload[:8]


def test_verify_rejects_paths_outside_the_task_directory(tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)
    output = _deliver(source, allowed, cwd, set(), task_id="alpha")
    outside = tmp_path / "other.png"
    outside.write_bytes(payload)
    foreign = output.model_copy(
        update={"path": str(outside), "mime": "image/png", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    )

    with pytest.raises(ValueError, match="traversal"):
        verify_output(output.model_copy(update={"path": "../" + output.path}), cwd, "alpha")
    with pytest.raises(ValueError, match="outside"):
        verify_output(output, cwd, "beta")
    with pytest.raises(ValueError, match="outside"):
        verify_output(foreign, cwd, "alpha")

    verify_output(output, cwd, "alpha")
    assert outside.read_bytes() == payload


def test_corrupted_copy_is_not_published(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)
    real_open = Path.open

    def open_and_corrupt(self: Path, mode: str = "r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if mode == "x+b":
            original = handle.write

            def write(chunk: bytes) -> int:
                if len(chunk) > 40:
                    mutable = bytearray(chunk)
                    mutable[30] ^= 0x01
                    chunk = bytes(mutable)
                return original(chunk)

            handle.write = write
        return handle

    monkeypatch.setattr(Path, "open", open_and_corrupt)

    with pytest.raises(ValueError, match=r"media changed during (copy|validation)"):
        _deliver(source, allowed, cwd, set())

    assert _published_files(cwd) == []


def test_copy_write_failure_leaves_no_partial_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    source.write_bytes(png_bytes())
    real_open = Path.open

    def open_and_fail(self: Path, mode: str = "r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if mode == "x+b":

            def write(_chunk: bytes) -> int:
                raise OSError(28, "no space")

            handle.write = write
        return handle

    monkeypatch.setattr(Path, "open", open_and_fail)

    with pytest.raises(OSError, match="no space"):
        _deliver(source, allowed, cwd, set())

    assert _published_files(cwd) == []
def test_source_replaced_before_open_is_not_published(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    allowed, cwd = _workspace(tmp_path)
    source = allowed / "frame.png"
    payload = png_bytes()
    source.write_bytes(payload)
    real_stat = Path.stat

    def replaced_stat(self: Path, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self == source:
            return type("ReplacedStat", (), {"st_mode": info.st_mode, "st_dev": info.st_dev, "st_ino": info.st_ino + 1})()
        return info

    monkeypatch.setattr(Path, "stat", replaced_stat)

    with pytest.raises(ValueError, match="changed before it could be opened"):
        _deliver(source, allowed, cwd, set())

    assert source.read_bytes() == payload
    assert _published_files(cwd) == []


def test_existing_files_stops_above_the_inventory_cap(tmp_path: Path):
    allowed, _cwd = _workspace(tmp_path)
    for index in range(4096):
        (allowed / f"f{index}.bin").write_bytes(b"x")

    assert len(existing_files(allowed)) == 4096
    (allowed / "overflow.bin").write_bytes(b"x")
    with pytest.raises(ValueError, match="inventory limit"):
        existing_files(allowed)
