import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("PDCST_TEST_WHEEL") != "1",
    reason="Set PDCST_TEST_WHEEL=1 after installing a built wheel",
)
def test_installed_import_and_help_outside_checkout(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, PDCST_HOME=str(home))
    imported = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import pdcst; from pathlib import Path; "
            "from sysconfig import get_paths; "
            "assert Path(get_paths()['purelib']) in Path(pdcst.__file__).parents",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr
    help_result = subprocess.run(
        [str(Path(sys.executable).with_name("pdcst")), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "Usage:" in help_result.stdout
    assert not list(home.iterdir())
