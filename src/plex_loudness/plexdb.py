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


def _docker(*args: str) -> str:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise PlexDBError("The docker CLI isn't available here. The controller needs it (or the docker socket"
                          " mounted into its container) to reach the Plex container.") from None
    if proc.returncode != 0:
        raise PlexDBError(f"docker {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


class PlexDB:
    def __init__(self, config: PlexConfig):
        self.config = config
        self.container = config.container
        self.user = config.user

    def detect(self) -> None:
        """Resolve 'auto' container and user (docker backend)."""
        if self.config.backend != "docker":
            return
        if self.container in ("", "auto"):
            rows = [line.split("\t") for line in _docker("ps", "--format", "{{.Names}}\t{{.Image}}").splitlines()]
            maybe = sorted({name for name, image in rows
                            if any(word in (name + image).lower() for word in ("plex", "pms"))
                            and "loudness" not in image.lower()},
                           key=lambda n: (n.lower() != "plex", n))
            # other Plex-related apps match by name too; the real server is the one with Plex SQLite
            found = [name for name in maybe if subprocess.run(
                ["docker", "exec", name, "test", "-x", self.config.sqlite], capture_output=True).returncode == 0]
            if not found:
                raise PlexDBError("Couldn't find a running Plex container (one with Plex SQLite)."
                                  " Set PLEX_CONTAINER to its name.")
            self.container = found[0]
        if self.user in ("", "auto"):
            owner = _docker("exec", self.container, "stat", "-c", "%u:%g", self.config.database)
            if not owner or ":" not in owner:
                raise PlexDBError(f"No Plex database at {self.config.database} in container {self.container}."
                                  " Set PLEX_DATABASE (and PLEX_SQLITE) if your install differs.")
            self.user = owner

    def describe(self) -> str:
        if self.config.backend == "local":
            return f"local {self.config.database}"
        return f"container {self.container} as {self.user}"

    def _prefix(self) -> list[str]:
        if self.config.backend == "docker":
            if self.container in ("", "auto") or self.user in ("", "auto"):
                self.detect()
            return ["docker", "exec", "-i", "-u", self.user, self.container]
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
