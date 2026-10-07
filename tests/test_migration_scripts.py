"""Static checks for VPS migration scripts."""
from __future__ import annotations

import pytest
pytest.skip('Private production migration scripts are not part of the source distribution', allow_module_level=True)

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DIR = PROJECT_ROOT / "deploy" / "migration"
SCRIPTS = [
    MIGRATION_DIR / "export_bundle.sh",
    MIGRATION_DIR / "restore_bundle.sh",
    MIGRATION_DIR / "smoke_check.sh",
]


def test_migration_scripts_exist_and_are_executable() -> None:
    for script in SCRIPTS:
        assert script.is_file(), f"missing migration script: {script}"
        assert script.stat().st_mode & 0o111, f"script is not executable: {script}"


def test_migration_scripts_pass_bash_syntax_check() -> None:
    for script in SCRIPTS:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr


def test_migration_scripts_have_help_output() -> None:
    for script in SCRIPTS:
        result = subprocess.run(
            ["bash", str(script), "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout

