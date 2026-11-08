"""Configuration: one TOML file shared by the controller and the workers.

Search order: --config, $PLEX_LOUDNESS_CONFIG, ./config.toml,
~/.config/plex-loudness/config.toml, /config/config.toml.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE = ("/config/Library/Application Support/Plex Media Server/Plug-in Support/"
                    "Databases/com.plexapp.plugins.library.db")
DEFAULT_SQLITE = "/usr/lib/plexmediaserver/Plex SQLite"
SEARCH_PATHS = ["config.toml", "~/.config/plex-loudness/config.toml", "/config/config.toml"]


@dataclass
class PlexConfig:
    backend: str = "docker"          # "docker": run Plex SQLite inside the Plex container; "local": run it here
    container: str = "plex"          # docker backend: Plex container name
    user: str = "abc"                # docker backend: user that owns the database inside the container
    sqlite: str = DEFAULT_SQLITE     # path to Plex SQLite (inside the container for the docker backend)
    database: str = DEFAULT_DATABASE  # library database path (inside the container for the docker backend)
    backup_dir: str = ""             # where backups go (same filesystem view as `database`); default: next to it


@dataclass
class WorkerConfig:
    name: str = ""                   # shown in the TUI; default: hostname
    workers: int = 0                 # parallel ffmpeg processes; 0 = CPU count - 2
    poll_seconds: float = 30         # how often an idle worker looks for new batches
    timeout_factor: float = 1.5      # per-track timeout = duration * factor + timeout_base
    timeout_base: float = 1800
    path_map: dict[str, str] = field(default_factory=dict)  # Plex path prefix -> path on this worker


@dataclass
class Config:
    work_dir: str = "work"           # the shared folder (controller and workers must both see it)
    plex: PlexConfig = field(default_factory=PlexConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    source: str = ""                 # file this was loaded from ("" = defaults)

    @property
    def batches_dir(self) -> Path:
        return Path(self.work_dir) / "batches"

    @property
    def worker_name(self) -> str:
        return self.worker.name or socket.gethostname()

    @property
    def worker_count(self) -> int:
        return self.worker.workers or max(1, (os.cpu_count() or 2) - 2)


def _apply(target, values: dict, section: str) -> None:
    for key, value in values.items():
        if not hasattr(target, key):
            raise SystemExit(f"Unknown setting [{section}] {key}")
        setattr(target, key, value)


def load(path: str | None = None) -> Config:
    """Load the config file (if any) and apply environment overrides."""
    candidates = [path] if path else [os.environ.get("PLEX_LOUDNESS_CONFIG", "")] + SEARCH_PATHS
    config = Config()
    for candidate in candidates:
        if not candidate:
            continue
        file = Path(candidate).expanduser()
        if file.is_file():
            with open(file, "rb") as handle:
                data = tomllib.load(handle)
            config.work_dir = data.get("work_dir", config.work_dir)
            _apply(config.plex, data.get("plex", {}), "plex")
            worker = dict(data.get("worker", {}))
            path_map = worker.pop("path_map", {})
            _apply(config.worker, worker, "worker")
            config.worker.path_map = dict(path_map)
            config.source = str(file)
            break
        if path:
            raise SystemExit(f"Config file not found: {path}")
    if os.environ.get("PLEX_LOUDNESS_WORK"):
        config.work_dir = os.environ["PLEX_LOUDNESS_WORK"]
    if os.environ.get("PLEX_LOUDNESS_WORKERS"):
        config.worker.workers = int(os.environ["PLEX_LOUDNESS_WORKERS"])
    if os.environ.get("PLEX_LOUDNESS_WORKER_NAME"):
        config.worker.name = os.environ["PLEX_LOUDNESS_WORKER_NAME"]
    return config
