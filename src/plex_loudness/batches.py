"""The shared-folder queue between the controller and any number of workers.

    <work_dir>/batches/<batch id>/
        batch.json            what to measure (written by the controller)
        claims/<stream id>    a worker is measuring this track (refreshed as a heartbeat)
        results/<worker>.jsonl one line per measured track, per worker (no shared appends)
        cancelled             marker: workers stop taking tracks from this batch
        paused                marker: workers freeze this batch's running tracks and take no new ones
        imported.json         marker: results were written into Plex
    <work_dir>/workers/<worker>.json   live status of each worker
    <work_dir>/workers/<worker>.paused marker: that worker freezes everything and takes nothing new

Claims use O_CREAT|O_EXCL, which is atomic on local disks and NFSv4, so two workers never
measure the same track. A claim that hasn't been refreshed for STALE_SECONDS is taken over.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import time
from pathlib import Path

STALE_SECONDS = 600
WORKER_ALIVE_SECONDS = 90


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as handle:
        json.dump(data, handle)
    _shared(tmp, 0o666)
    os.replace(tmp, path)


def _shared(path: Path, mode: int = 0o777) -> None:
    """Controller and workers may run as different users: keep the shared folder writable for both."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _shared(path)


def _read_json(path: Path, default=None):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "batch"


class Batch:
    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def data(self) -> dict:
        return _read_json(self.path / "batch.json", {})

    @property
    def cancelled(self) -> bool:
        return (self.path / "cancelled").exists()

    @property
    def paused(self) -> bool:
        return (self.path / "paused").exists()

    @property
    def imported(self) -> dict | None:
        return _read_json(self.path / "imported.json")

    def tracks(self) -> list[tuple[dict, dict]]:
        """(job, stream) pairs: every audio track in the batch."""
        return [(job, stream) for job in self.data.get("jobs", []) for stream in job["streams"]]

    def results(self) -> dict[int, dict]:
        """Latest result per stream id, merged across workers."""
        merged: dict[int, dict] = {}
        folder = self.path / "results"
        for file in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
            with open(file, errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    old = merged.get(record["stream_id"])
                    if old is None or (record.get("retry", 0), record.get("at", 0)) >= (old.get("retry", 0), old.get("at", 0)):
                        merged[record["stream_id"]] = record
        return merged

    def add_result(self, worker: str, record: dict) -> None:
        folder = self.path / "results"
        _mkdir(folder)
        file = folder / f"{slug(worker)}.jsonl"
        new = not file.exists()
        with open(file, "a") as handle:
            handle.write(json.dumps(record) + "\n")
        if new:
            _shared(file, 0o666)

    # ---- claims
    def _claim_path(self, stream_id: int) -> Path:
        return self.path / "claims" / str(stream_id)

    def claim(self, stream_id: int, worker: str) -> bool:
        folder = self.path / "claims"
        _mkdir(folder)
        path = self._claim_path(stream_id)
        try:
            if time.time() - path.stat().st_mtime > STALE_SECONDS:
                path.unlink()  # its worker died or lost the share; take it over
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            json.dump({"worker": worker, "at": time.time()}, handle)
        return True

    def touch_claim(self, stream_id: int) -> None:
        try:
            os.utime(self._claim_path(stream_id))
        except OSError:
            pass

    def release(self, stream_id: int) -> None:
        try:
            self._claim_path(stream_id).unlink()
        except OSError:
            pass

    def live_claims(self) -> set[int]:
        folder = self.path / "claims"
        now = time.time()
        out = set()
        for path in folder.iterdir() if folder.is_dir() else []:
            try:
                if now - path.stat().st_mtime <= STALE_SECONDS and path.name.isdigit():
                    out.add(int(path.name))
            except OSError:
                pass
        return out

    def pending(self, results: dict | None = None) -> list[tuple[dict, dict]]:
        """Tracks that still need measuring (no result, or failed before the latest retry)."""
        if self.cancelled or self.paused or self.imported:
            return []
        results = self.results() if results is None else results
        retry = self.data.get("retry", 0)
        out = []
        for job, stream in self.tracks():
            record = results.get(stream["stream_id"])
            if record is None or record["status"] == "stopped" or (
                    record["status"] != "ok" and record.get("retry", 0) < retry):
                out.append((job, stream))
        return out

    # ---- controller actions
    def cancel(self) -> None:
        (self.path / "cancelled").touch()
        self.unpause()          # frozen tracks wake up and finish rather than hang on to their claims

    def resume(self) -> None:
        """Undo cancel and pause: workers pick the batch up again."""
        for marker in ("cancelled", "paused"):
            try:
                (self.path / marker).unlink()
            except FileNotFoundError:
                pass

    def pause(self) -> None:
        (self.path / "paused").touch()
        _shared(self.path / "paused", 0o666)

    def unpause(self) -> None:
        try:
            (self.path / "paused").unlink()
        except FileNotFoundError:
            pass

    def retry_failed(self) -> int:
        data = self.data
        data["retry"] = data.get("retry", 0) + 1
        _write_json(self.path / "batch.json", data)
        failed = [sid for sid, r in self.results().items() if r["status"] != "ok"]
        for sid in failed:
            self.release(sid)
        self.resume()
        return len(failed)

    def mark_imported(self, info: dict) -> None:
        _write_json(self.path / "imported.json", dict(info, at=time.time()))

    def summary(self) -> dict:
        data, results = self.data, self.results()
        tracks = self.tracks()
        by_id = {stream["stream_id"]: job for job, stream in tracks}
        ok = sum(1 for sid, r in results.items() if sid in by_id and r["status"] == "ok")
        failed = sum(1 for sid, r in results.items() if sid in by_id and r["status"] == "failed")
        total_h = sum(job["duration_ms"] for job, _ in tracks) / 3_600_000
        done_h = sum(by_id[sid]["duration_ms"] for sid, r in results.items()
                     if sid in by_id and r["status"] in ("ok", "failed")) / 3_600_000
        running = len(self.live_claims())
        if self.imported:
            state = "imported"
        elif self.cancelled:
            state = "cancelled"
        elif ok + failed >= len(tracks):
            state = "finished"
        elif self.paused:
            state = "paused"
        elif running:
            state = "running"
        else:
            state = "queued"
        return {"id": self.id, "name": data.get("name", ""), "created": data.get("created", 0),
                "mode": data.get("mode", {}).get("name", "full"), "filter": data.get("filter", ""),
                "files": len(data.get("jobs", [])), "tracks": len(tracks), "ok": ok, "failed": failed,
                "running": running, "hours": total_h, "hours_done": done_h, "state": state,
                "imported": self.imported}


class Queue:
    def __init__(self, work_dir: str | Path):
        self.work = Path(work_dir)
        self.root = self.work / "batches"
        self.workers_dir = self.work / "workers"

    def ensure(self) -> None:
        _mkdir(self.root)
        _mkdir(self.workers_dir)

    def batches(self) -> list[Batch]:
        if not self.root.is_dir():
            return []
        return [Batch(p) for p in sorted(self.root.iterdir()) if (p / "batch.json").is_file()]

    def get(self, batch_id: str) -> Batch:
        batches = self.batches()
        matches = [b for b in batches if b.id == batch_id or b.id.startswith(batch_id)]
        if not matches:   # also by name: "movies" finds 20261110-163223-movies (the newest if several)
            named = [b for b in batches if b.id.split("-", 2)[-1] == slug(batch_id)]
            matches = named[-1:]
        if len(matches) != 1:
            known = ", ".join(b.id for b in self.batches()[-8:]) or "none"
            raise SystemExit(f"{'No' if not matches else 'More than one'} batch matching {batch_id!r} (recent: {known})")
        return matches[0]

    def latest(self) -> Batch | None:
        batches = self.batches()
        return batches[-1] if batches else None

    def create(self, name: str, jobs: list[dict], mode: dict, description: str, created_by: str) -> Batch:
        self.ensure()
        batch_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug(name)}"
        path = self.root / batch_id
        _mkdir(path)
        _mkdir(path / "claims")
        _mkdir(path / "results")
        _write_json(path / "batch.json", {"id": batch_id, "name": name, "created": time.time(),
                                          "created_by": created_by, "mode": mode, "filter": description,
                                          "retry": 0, "jobs": jobs})
        return Batch(path)

    def delete(self, batch: Batch) -> None:
        shutil.rmtree(batch.path)

    # ---- worker heartbeats
    def write_worker(self, name: str, status: dict) -> None:
        self.ensure()
        _write_json(self.workers_dir / f"{slug(name)}.json", dict(status, name=name, updated=time.time()))

    def workers(self) -> list[dict]:
        out = []
        for path in sorted(self.workers_dir.glob("*.json")) if self.workers_dir.is_dir() else []:
            status = _read_json(path)
            if status:
                status["alive"] = time.time() - status.get("updated", 0) < WORKER_ALIVE_SECONDS
                status["pause_requested"] = self.worker_paused(status.get("name", path.stem))
                out.append(status)
        return out

    def get_worker(self, name: str) -> dict:
        """A worker by name (or unique prefix), for the pause/resume commands."""
        workers = self.workers()
        matches = [w for w in workers if w["name"] == name] or [w for w in workers if w["name"].startswith(name)]
        if len(matches) != 1:
            known = ", ".join(w["name"] for w in workers) or "none"
            raise SystemExit(f"{'No' if not matches else 'More than one'} worker matching {name!r} (known: {known})")
        return matches[0]

    # ---- pausing a whole worker (e.g. the desktop while you need its CPU)
    def _pause_path(self, name: str) -> Path:
        return self.workers_dir / f"{slug(name)}.paused"

    def worker_paused(self, name: str) -> bool:
        return self._pause_path(name).exists()

    def pause_worker(self, name: str) -> None:
        self.ensure()
        self._pause_path(name).touch()
        _shared(self._pause_path(name), 0o666)

    def resume_worker(self, name: str) -> None:
        try:
            self._pause_path(name).unlink()
        except FileNotFoundError:
            pass
