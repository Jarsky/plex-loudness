"""Configuration, from environment variables (the Docker way) and/or an optional TOML file.

Environment variables win over the file. In Docker everything is normally set in `.env`:

    ROLE            controller | worker | both                      (entrypoint only)
    WORKERS         parallel tracks on this worker (0 = CPU count - 2)
    WORKER_NAME     shown in the controller (default: hostname)
    STALL_SECONDS   kill a track whose ffmpeg makes no progress for this long (default 900)
    TIMEOUT_FACTOR  hard limit per track = duration x factor + 30 min (default 4)
    PATH_MAP        optional: "/tv=/media/tv_shows,/movies=/media/movies" (default: auto-detect)
    PLEX_CONTAINER  Plex container name (default: auto-detect)
    PLEX_USER       user that owns the Plex database (default: auto-detect)
    PLEX_BACKEND    docker | local
    PLEX_DATABASE, PLEX_SQLITE, BACKUP_DIR   only if your Plex install differs from the usual
    WORK_DIR        the shared folder (in Docker always /work)

Optional file: --config, $PLEX_LOUDNESS_CONFIG, ./config.toml, ~/.config/plex-loudness/config.toml,
/config/config.toml (see config.example.toml).
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
    container: str = "auto"          # docker backend: Plex container name, or auto-detect
    user: str = "auto"               # docker backend: database owner inside the container, or auto-detect
    sqlite: str = DEFAULT_SQLITE
    database: str = DEFAULT_DATABASE
    backup_dir: str = ""             # default: next to the database


@dataclass
class WorkerConfig:
    name: str = ""                   # default: hostname
    workers: int = 0                 # 0 = CPU count - 2
    poll_seconds: float = 30
    # A track is killed if ffmpeg makes no progress for stall_seconds (stuck or runaway), or runs past
    # duration * timeout_factor + timeout_base. The factor is generous because a busy host can run well
    # under real time (seen: 0.56x for TrueHD 7.1 with the NAS at 2x load) and that is slow, not broken.
    timeout_factor: float = 4.0
    timeout_base: float = 1800
    stall_seconds: float = 900
    media_root: str = "/media"       # where auto path detection looks
    path_map: dict[str, str] = field(default_factory=dict)  # Plex path prefix -> path on this worker


@dataclass
class Config:
    work_dir: str = "work"
    plex: PlexConfig = field(default_factory=PlexConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    source: str = ""

    @property
    def batches_dir(self) -> Path:
        return Path(self.work_dir) / "batches"

    @property
    def worker_name(self) -> str:
        return self.worker.name or socket.gethostname()

    @property
    def worker_count(self) -> int:
        return self.worker.workers or max(1, (os.cpu_count() or 2) - 2)


def parse_path_map(text: str) -> dict[str, str]:
    """'/tv=/media/tv_shows, /movies=/media/movies' -> dict."""
    out = {}
    for pair in (text or "").replace(";", ",").split(","):
        if "=" in pair:
            src, dst = pair.split("=", 1)
            if src.strip() and dst.strip():
                out[src.strip()] = dst.strip()
    return out


def _apply(target, values: dict, section: str) -> None:
    for key, value in values.items():
        if not hasattr(target, key):
            raise SystemExit(f"Unknown setting [{section}] {key}")
        setattr(target, key, value)


ENV = {  # variable -> (section, attribute, type)
    "WORK_DIR": (None, "work_dir", str),
    "PLEX_BACKEND": ("plex", "backend", str),
    "PLEX_CONTAINER": ("plex", "container", str),
    "PLEX_USER": ("plex", "user", str),
    "PLEX_SQLITE": ("plex", "sqlite", str),
    "PLEX_DATABASE": ("plex", "database", str),
    "BACKUP_DIR": ("plex", "backup_dir", str),
    "WORKERS": ("worker", "workers", int),
    "WORKER_NAME": ("worker", "name", str),
    "POLL_SECONDS": ("worker", "poll_seconds", float),
    "TIMEOUT_FACTOR": ("worker", "timeout_factor", float),
    "STALL_SECONDS": ("worker", "stall_seconds", float),
    "MEDIA_ROOT": ("worker", "media_root", str),
}


def load(path: str | None = None) -> Config:
    config = Config()
    candidates = [path] if path else [os.environ.get("PLEX_LOUDNESS_CONFIG", "")] + SEARCH_PATHS
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
    for var, (section, attr, kind) in ENV.items():
        value = os.environ.get(var) or os.environ.get(f"PLEX_LOUDNESS_{var}")
        if value:
            setattr(getattr(config, section) if section else config, attr, kind(value))
    if os.environ.get("PATH_MAP"):
        config.worker.path_map = parse_path_map(os.environ["PATH_MAP"])
    if not config.source and any(os.environ.get(v) for v in ENV):
        config.source = "environment"
    return config
