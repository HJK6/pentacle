import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


def test_install_sh_exists_and_is_executable():
    assert INSTALL_SH.exists()
    assert os.access(INSTALL_SH, os.X_OK)


def test_install_sh_help_exits_zero():
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_install_sh_dry_run_exits_zero():
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "install command:" in result.stdout
    assert "dry run: no install performed" in result.stdout
