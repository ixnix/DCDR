"""The built wheel must work when installed, outside any checkout of this repo.

Reproduces the step-1 failure from the packaging exercise: an installed `dcdr status`
crashed because default.yaml lived outside the package.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs the uv executable")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out), str(REPO)],
        check=True,
        capture_output=True,
    )
    (whl,) = out.glob("dcdr-*.whl")
    return whl


def _installed(wheel: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a command in a throwaway environment that has only the wheel installed."""
    env = {k: v for k, v in os.environ.items() if k not in ("DCDR_DATA", "VIRTUAL_ENV")}
    cmd = ["uv", "run", "--isolated", "--no-project", "--with", str(wheel), *args]
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def test_wheel_ships_sql_and_default_config(wheel: Path) -> None:
    names = zipfile.ZipFile(wheel).namelist()
    assert "dcdr/sql/daily.sql" in names
    assert "dcdr/default.yaml" in names


def test_installed_dcdr_runs_outside_the_repo(wheel: Path, tmp_path: Path) -> None:
    status = _installed(wheel, tmp_path, "dcdr", "status")
    assert status.returncode == 0, status.stderr
    assert "no source manifest yet" in status.stdout

    check = _installed(
        wheel,
        tmp_path,
        "python",
        "-c",
        "from dcdr.config import load_settings; from dcdr.gold import SQL_DIR; "
        "s = load_settings(); print(s.data_root); print(len((SQL_DIR / 'daily.sql').read_text()))",
    )
    assert check.returncode == 0, check.stderr
    data_root, sql_chars = check.stdout.split()
    assert Path(data_root) == tmp_path.resolve() / "data"  # relative to where it runs
    assert int(sql_chars) > 0


def test_installed_version_comes_from_package_metadata(wheel: Path, tmp_path: Path) -> None:
    from dcdr import __version__

    out = _installed(wheel, tmp_path, "dcdr", "--version")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"dcdr {__version__}"
