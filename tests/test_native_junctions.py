"""Real Windows reparse-point checks, without symbolic-link privileges."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys

import pytest

from agent_bridge.output_files import checked_path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_actual_directory_junction_is_rejected(tmp_path):
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable for Windows junction fixture")
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "frame.png").write_bytes(b"fixture")
    junction = allowed / "images"
    # Paths come solely from this test's isolated temporary directory.
    def run(script):
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, check=True, timeout=15,
        )
    link_arg = str(junction).replace("'", "''")
    target_arg = str(outside).replace("'", "''")
    run(f"New-Item -ItemType Junction -Path '{link_arg}' -Target '{target_arg}' -ErrorAction Stop | Out-Null")
    try:
        with pytest.raises(ValueError, match=r"reparse|links"):
            checked_path(junction / "frame.png", allowed)
        with pytest.raises(ValueError, match=r"reparse|links"):
            checked_path(junction, allowed, regular=False)
    finally:
        # Remove only the link itself; the target stays inside the fixture.
        run(f"Remove-Item -LiteralPath '{link_arg}' -Force -ErrorAction Stop")
    assert (outside / "frame.png").read_bytes() == b"fixture"
