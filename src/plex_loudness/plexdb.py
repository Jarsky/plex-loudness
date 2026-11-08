"""Access to the Plex library database through Plex's own SQLite build.

Plex SQLite is used for every read and write (Plex's database relies on its build), run
either inside the Plex container (`docker exec`, as the user that owns the files) or
directly on this machine.
"""

from __future__ import annotations

import datetime as dt
import json
import posixpath
import subprocess
import sys

from .config import PlexConfig


class PlexDBError(SystemExit):
    pass


def sql_text(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def sql_ids(ids) -> str:
    return ",".join(str(int(i)) for i in ids) or "NULL"


class PlexDB:
    def __init__(self, config: PlexConfig):
        self.config = config

    def _prefix(self) -> list[str]:
        if self.config.backend == "docker":
            return ["docker", "exec", "-i", "-u", self.config.user, self.config.container]
        if self.config.backend == "local":
            return []
        raise PlexDBError(f"Unknown plex.backend {self.config.backend!r} (use 'docker' or 'local')")

    def run(self, sql: str, readonly: bool = True, timeout: float = 600) -> str:
        cmd = self._prefix() + [self.config.sqlite] + (["-readonly"] if readonly else []) + [self.config.database]
        try:
            proc = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise PlexDBError(
                f"Can't run {exc.filename!r}. The controller needs the Plex database: run it where Plex runs\n"
                "(docker backend: the docker CLI and access to the Plex container; local backend: Plex SQLite).\n"
                "Workers only analyse audio and never touch the database.") from None
        if proc.returncode != 0 or proc.stderr.strip().startswith(("Error", "Parse error", "Runtime error")):
            raise PlexDBError(f"Plex SQLite failed: {(proc.stderr or proc.stdout).strip()[:500]}")
        return proc.stdout

    def query(self, sql: str) -> list[dict]:
        out = self.run(".mode json\n" + sql.strip().rstrip(";") + ";\n").strip()
        return json.loads(out) if out else []

    def _exec(self, *args: str) -> str:
        proc = subprocess.run(self._prefix() + list(args), capture_output=True, text=True)
        return proc.stdout.strip()

    def backup(self) -> str:
        """Online backup with SQLite's .backup (safe while Plex runs). Returns the backup path."""
        folder = self.config.backup_dir or posixpath.dirname(self.config.database)
        name = posixpath.basename(self.config.database)
        target = posixpath.join(folder, f"{name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}-plex-loudness")
        self.run(f".timeout 30000\n.backup {sql_text(target)}\n", readonly=True, timeout=3600)
        size = self._exec("stat", "-c", "%s", target)
        if not size.isdigit():
            raise PlexDBError(f"Backup did not appear at {target}")
        print(f"Backup written: {target} ({int(size) / 1e6:,.0f} MB)", file=sys.stderr)
        return target
