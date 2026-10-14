"""Runtime configuration for LithoCreep.

Values are read from environment variables (or ``backend/.env`` / a ``.env``
in the working directory) via pydantic-settings:

* ``EARTHDATA_USERNAME`` / ``EARTHDATA_PASSWORD`` - NASA Earthdata Login
  credentials used by ``earthaccess``. Both default to ``None``; without them
  ``earthaccess`` falls back to ``~/.netrc``.
* ``CORS_ORIGINS`` - comma-separated origins allowed to call the API.
* ``DATA_DIR`` - root of the local data store (default ``"../data"``). A
  relative path is resolved against the ``backend/`` directory, so the default
  always points at ``<repo>/data`` regardless of the current working directory.

The data directory and its working sub-folders are created and checked for
write access when the settings object is built; a ``PermissionError`` is raised
if any of them is not writable.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Sub-folders of DATA_DIR used by the pipelines.
DATA_SUBDIRS = ("raw", "cache", "processed", "mock", "logs")


def _assert_writable(path: Path) -> None:
    """Raise ``PermissionError`` unless ``path`` is a writable directory."""
    if not path.is_dir():
        raise NotADirectoryError(f"Data path is not a directory: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise PermissionError(f"Write permission missing for data directory: {path}")
    # os.access is advisory (e.g. read-only mounts, ACLs); confirm with a real write.
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".write_probe_"):
            pass
    except OSError as exc:
        raise PermissionError(f"Write permission missing for data directory: {path} ({exc})") from exc


class Settings(BaseSettings):
    # backend/.env is always read; a .env in the working directory overrides it.
    model_config = SettingsConfigDict(env_file=(BACKEND_DIR / ".env", ".env"), env_file_encoding="utf-8", extra="ignore")

    EARTHDATA_USERNAME: Optional[str] = None
    EARTHDATA_PASSWORD: Optional[str] = None
    DATA_DIR: str = "../data"
    # Comma-separated origins allowed by the API's CORS policy (the Next.js dev server by default).
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    @model_validator(mode="after")
    def _validate_data_dir(self) -> "Settings":
        data_path = Path(self.DATA_DIR).expanduser()
        if not data_path.is_absolute():
            data_path = BACKEND_DIR / data_path
        data_path = data_path.resolve()

        for folder in (data_path, *(data_path / name for name in DATA_SUBDIRS)):
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PermissionError(f"Cannot create data directory {folder}: {exc}") from exc
            _assert_writable(folder)

        self.DATA_DIR = str(data_path)
        return self

    @property
    def data_path(self) -> Path:
        return Path(self.DATA_DIR)

    @property
    def raw_dir(self) -> Path:
        return self.data_path / "raw"

    @property
    def cache_dir(self) -> Path:
        return self.data_path / "cache"

    @property
    def processed_dir(self) -> Path:
        return self.data_path / "processed"

    @property
    def mock_dir(self) -> Path:
        return self.data_path / "mock"

    @property
    def log_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def has_earthdata_credentials(self) -> bool:
        return bool(self.EARTHDATA_USERNAME and self.EARTHDATA_PASSWORD)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
