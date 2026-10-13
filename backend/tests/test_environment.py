"""Check that the active interpreter matches the pins in backend/requirements.txt exactly."""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"
PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)")


def pinned_requirements() -> list[tuple[str, str]]:
    pins = []
    for line in REQUIREMENTS.read_text().splitlines():
        match = PIN_RE.match(line.strip())
        if match:
            pins.append((match.group(1), match.group(2)))
    return pins


def test_requirements_are_fully_pinned():
    lines = [l.strip() for l in REQUIREMENTS.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert lines and all(PIN_RE.match(l) for l in lines), "every requirement must be pinned with =="


def test_python_version_supported():
    # duckdb 0.9.2 has no CPython 3.12 wheel (source build fails); scipy 1.11.4 needs >= 3.9.
    assert (3, 9) <= sys.version_info[:2] <= (3, 11)


@pytest.mark.parametrize("name,version", pinned_requirements())
def test_installed_version_matches_pin(name, version):
    try:
        installed = metadata.version(name)
    except metadata.PackageNotFoundError:
        pytest.fail(f"{name} is not installed (pinned {version})")
    assert installed == version, f"{name}: installed {installed}, pinned {version}"
