"""Repository hygiene: no hardcoded secrets, and synthetic data stays out of the real-data paths.

Scans every file git would commit (tracked plus untracked-but-not-ignored), so it also guards
work in progress. Patterns cover the credentials this project could plausibly hold: Anthropic,
Mapbox, AWS, GitHub, Slack, private keys, and NASA Earthdata passwords in config files.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SECRET_PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "Mapbox token": re.compile(r"\b[ps]k\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}
# KEY=value assignments in env / config files must be empty or an obvious placeholder.
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*(?:PASSWORD|TOKEN|API_KEY|SECRET)[A-Z0-9_]*)\s*=\s*[\"']?([^\"'\s#]*)", re.M)
PLACEHOLDER = re.compile(r"^(?:|your_[a-z_]+|<[^>]+>|\$\{?[A-Z_]+\}?|changeme|xxx+)$", re.I)
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".mts", ".json", ".md", ".txt", ".csv", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".css", ".html", ".geojson", ".example", ""}


def repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file()]


def text_files() -> list[Path]:
    files = []
    for p in repo_files():
        if p.suffix in TEXT_SUFFIXES or p.name.startswith(".env"):
            try:
                if p.stat().st_size <= 5_000_000:
                    files.append(p)
            except OSError:
                pass
    return files


def test_scan_covers_the_repository():
    names = {p.relative_to(ROOT).as_posix() for p in text_files()}
    assert {"backend/app/config.py", "frontend/next.config.js", "frontend/package.json", "IMPLEMENTATION_STATUS.md"} <= names


@pytest.mark.parametrize("label", sorted(SECRET_PATTERNS))
def test_no_hardcoded_secrets(label):
    pattern = SECRET_PATTERNS[label]
    hits = []
    for p in text_files():
        text = p.read_text(errors="ignore")
        for m in pattern.finditer(text):
            hits.append(f"{p.relative_to(ROOT)}: {m.group(0)[:12]}…")
    assert not hits, f"{label} found: {hits}"


def test_no_env_files_committed_and_examples_are_placeholders():
    files = repo_files()
    env = [p for p in files if p.name.startswith(".env") and not p.name.endswith(".example")]
    assert not env, f"environment files must not be committed: {env}"
    for p in [p for p in files if p.name.endswith(".env.example") or p.suffix in {".sh", ".yml", ".yaml"} or p.name == "README.md"]:
        for name, value in ASSIGNMENT.findall(p.read_text(errors="ignore")):
            assert PLACEHOLDER.match(value), f"{p.relative_to(ROOT)}: {name} has a non-placeholder value"


def test_secrets_are_read_from_the_environment_only():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["EARTHDATA_USERNAME"].default is None and fields["EARTHDATA_PASSWORD"].default is None
    nextconfig = (ROOT / "frontend" / "next.config.js").read_text()
    assert "process.env.MAPBOX_ACCESS_TOKEN" in nextconfig and "pk." not in nextconfig


# --------------------------------------------------------------------------- #
# Data boundaries
# --------------------------------------------------------------------------- #


def test_no_synthetic_or_unpublished_series_in_the_real_data_paths():
    """The Brumadinho series is never bundled (it is not reconstructed or invented)."""
    tracked = {p.relative_to(ROOT).as_posix() for p in repo_files()}
    assert "data/brumadinho_historic.csv" not in tracked
    data_files = sorted(t for t in tracked if t.startswith("data/"))
    assert all(t.startswith("data/reference/") or t == "data/global_tailings.geojson" for t in data_files), data_files
    assert not any(t.endswith(".zarr") or "/cache/" in t for t in tracked)


def test_registry_holds_no_hazard_or_synthetic_claims():
    reg = json.loads((ROOT / "data" / "global_tailings.geojson").read_text())
    for f in reg["features"]:
        props = f["properties"]
        assert not any(k in props for k in ("hazard_level", "alert", "regime")), props["id"]
        assert "synthetic" not in json.dumps(props).lower(), props["id"]


def test_synthetic_generators_label_their_output():
    """Every synthetic writer used for demos/tests stamps its output as synthetic."""
    demo = (ROOT / "backend" / "scripts" / "build_demo_cache.py").read_text()
    assert "stamp_synthetic" in demo and "SYNTHETIC stand-in" in demo and "NOT the published" in demo
    scen = (ROOT / "backend" / "tests" / "test_scenarios.py").read_text()
    assert 'source: str = "SYNTHETIC' in scen
