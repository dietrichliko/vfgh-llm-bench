"""The app reports the version of pyproject.toml (which `cz bump` updates)."""

import tomllib
from pathlib import Path

import bench

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def test_version_follows_pyproject() -> None:
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert bench.__version__ == expected
