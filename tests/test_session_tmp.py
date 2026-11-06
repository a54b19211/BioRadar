"""The test session removes the temporary directory it created, also when a test fails."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def nested_session(tmp_path, test_source, scratch):
    """Run a nested pytest session with this conftest as its plugin; its TMPDIR is `scratch`."""
    test_file = tmp_path / "test_nested.py"
    test_file.write_text(test_source)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "conftest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--rootdir",
            str(ROOT),
            "-p",
            "no:cacheprovider",
            str(test_file),
        ],
        cwd=ROOT,
        env={"PATH": "", "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
    )


def scratch_dir(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return scratch


def test_the_session_directory_is_gone_after_a_passing_run(tmp_path):
    scratch = scratch_dir(tmp_path)
    result = nested_session(tmp_path, "def test_ok():\n    assert True\n", scratch)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(scratch.iterdir()) == []


def test_the_session_directory_is_gone_after_a_failing_run(tmp_path):
    scratch = scratch_dir(tmp_path)
    result = nested_session(tmp_path, "def test_boom():\n    assert False\n", scratch)
    assert result.returncode == 1, result.stdout + result.stderr
    assert list(scratch.iterdir()) == []


def test_a_directory_somebody_else_left_in_tmp_is_not_touched(tmp_path):
    scratch = scratch_dir(tmp_path)
    (scratch / "tmpforeign").mkdir()
    (scratch / "tmpforeign" / "test.db").write_text("x")
    result = nested_session(tmp_path, "def test_ok():\n    assert True\n", scratch)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (scratch / "tmpforeign" / "test.db").read_text() == "x"
    assert [p.name for p in scratch.iterdir()] == ["tmpforeign"]
