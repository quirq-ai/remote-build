import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path):
    """A plain directory holding the selftest fixture, standing in for a checkout."""
    (tmp_path / "selftest").mkdir()
    shutil.copyfile(ROOT / "selftest" / "words.txt", tmp_path / "selftest" / "words.txt")
    return tmp_path
