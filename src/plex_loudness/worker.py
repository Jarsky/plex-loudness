"""plex-loudness-worker: measure queued audio tracks. Run one on every machine that can see the media.

The worker never touches the Plex database. It reads batches from the shared folder, claims
tracks, runs ffmpeg, and writes results back for the controller to import.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import signal
import sys
import threading
import time

from . import __version__
from .analysis import Mode, measure_track, rss_mb
from .batches import Batch, Queue
from .config import Config, load
from .resources import Sampler
from .schedule import Schedule, load_schedule


def map_path(path: str, mapping: dict[str, str]) -> str:
    for src in sorted(mapping, key=len, reverse=True):
        if path == src or path.startswith(src.rstrip("/") + "/"):
            return mapping[src].rstrip("/") + path[len(src.rstrip("/")):]
    return path


class TrackStop:
    """Stops one track: when the worker stops, or when the track's batch has been deleted."""

    def __init__(self, worker_stop: threading.Event):
        self.worker_stop, self.dropped = worker_stop, threading.Event()
        self.missing = 0

    def is_set(self) -> bool:
        return self.worker_stop.is_set() or self.dropped.is_set()


class PathResolver:
    """Plex path -> path on this worker: explicit PATH_MAP first, else auto-detected.

    Auto-detection takes the file's top folder as Plex sees it (e.g. /tv) and looks for the
    folder under the media root (e.g. /media/tv_shows) where the same file really exists.
    The answer is remembered per top folder, so only the first file of each root is probed."""

    def __init__(self, mapping: dict[str, str], media_root: str = "/media", log=print):
        self.mapping = dict(mapping)
        self.media_root = media_root
        self.log = log
        self.lock = threading.Lock()

    def resolve(self, path: str) -> str:
        mapped = map_path(path, self.mapping)
        if mapped != path or os.path.exists(path):
            return mapped
        root = "/" + path.lstrip("/").split("/", 1)[0]
        rest = path[len(root):]
        with self.lock:
            if root in self.mapping:
                return map_path(path, self.mapping)
            try:
                folders = sorted(os.path.join(self.media_root, d) for d in os.listdir(self.media_root))
            except OSError:
                folders = []
            for folder in [self.media_root] + folders:
                if os.path.exists(folder + rest):
                    self.mapping[root] = folder
                    self.log(f"auto path map: {root} -> {folder}")
                    return folder + rest
        return path


class Worker:
    def __init__(self, config: Config, slots: int, only_batch: str | None = None, once: bool = False):
        self.config, self.slots, self.once = config, slots, once
        self.queue = Queue(config.work_dir)
        self.only_batch = only_batch
        self.name = config.worker_name
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active: dict[tuple[str, int], dict] = {}
        self.track_stops: dict[tuple[str, int], TrackStop] = {}
        self.stats = {"ok": 0, "failed": 0, "measured_s": 0.0, "audio_s": 0.0}
        self.started = time.time()
        # pause state, refreshed from the shared folder every loop (see refresh_pauses)
        self.self_paused = False
        self.paused_batches: set[str] = set()
        self.schedule = Schedule()
        self.off_hours = False             # outside the schedule's window: as if paused
        self.sampler = Sampler()           # CPU/memory of this container and its host, per heartbeat
        self.paths = PathResolver(config.worker.path_map, config.worker.media_root,
                                  log=lambda text: print(f"{time.strftime('%H:%M:%S')} {text}", flush=True))

    # ---- status
    def status(self) -> dict:
        with self.lock:
            active = [dict(a, rss_mb=rss_mb(a.get("pid"))) for a in self.active.values()]
            stats = dict(self.stats)
        elapsed = time.time() - self.started
        return {"version": __version__, "slots": self.slots, "started": self.started, "elapsed": elapsed,
                "active": sorted(active, key=lambda a: a["started"]), "stopping": self.stop.is_set(),
                "paused": self.self_paused, "off_hours": self.off_hours, "schedule": self.schedule.status(),
                "resources": self.sampler.sample(),
                "ok": stats["ok"], "failed": stats["failed"], "audio_hours": stats["audio_s"] / 3600,
                "rate": stats["audio_s"] / elapsed if elapsed > 60 else 0}

    def refresh_pauses(self) -> None:
        """Read the pause markers: this worker's own, and every batch's."""
        was = self.self_paused
        self.self_paused = self.queue.worker_paused(self.name)
        self.paused_batches = {b.id for b in self.queue.batches() if b.paused}
        if self.self_paused != was:
            print(f"{time.strftime('%H:%M:%S')} {'paused: running tracks frozen, taking no new ones' if self.self_paused else 'resumed'}",
                  flush=True)
        was_off, old = self.off_hours, self.schedule
        self.schedule = load_schedule(self.config.work_dir)
        self.off_hours = not self.schedule.is_open()
        if self.schedule != old:
            print(f"{time.strftime('%H:%M:%S')} schedule: {self.schedule.status()}", flush=True)
        elif self.off_hours != was_off:
            print(f"{time.strftime('%H:%M:%S')} schedule: {self.schedule.status()}"
                  f"{' (running tracks frozen until then)' if self.off_hours else ''}", flush=True)

    def drop_deleted(self) -> None:
        """Kill tracks whose batch was deleted from the controller, freeing their slots for other batches."""
        with self.lock:
            running = [(key, a["batch_path"], a["title"]) for key, a in self.active.items()]
        for key, batch_path, title in running:
            stop = self.track_stops.get(key)
            if not stop or stop.dropped.is_set():
                continue
            stop.missing = 0 if Batch(batch_path).exists() else stop.missing + 1
            if stop.missing >= 2:            # twice in a row, so a share hiccup doesn't kill a long track
                stop.dropped.set()
                print(f"{time.strftime('%H:%M:%S')} dropped  {title[:55]}: batch {key[0]} was deleted", flush=True)

    def is_paused(self, batch_id: str) -> bool:
        return self.self_paused or self.off_hours or batch_id in self.paused_batches

    def heartbeat(self) -> None:
        while not self.stop.wait(15):
            with self.lock:
                claims = [(a["batch_path"], a["stream_id"]) for a in self.active.values()]
            for batch_path, stream_id in claims:
                Batch(batch_path).touch_claim(stream_id)
            self.queue.write_worker(self.name, self.status())

    # ---- work
    def measure(self, batch: Batch, job: dict, stream: dict, mode: Mode, retry: int) -> None:
        key = (batch.id, stream["stream_id"])
        path = self.paths.resolve(job["file"])
        duration = (job.get("duration_ms") or 0) / 1000
        info = {"batch": batch.id, "batch_path": str(batch.path), "stream_id": stream["stream_id"],
                "title": job["title"], "codec": stream["codec"], "started": time.time(),
                "position": 0.0, "total": duration if mode.exact else mode.samples * mode.seconds, "pid": None,
                "paused_s": 0.0, "paused_since": None}
        track_stop = TrackStop(self.stop)
        with self.lock:
            self.active[key] = info
            self.track_stops[key] = track_stop

        def progress(position: float, pid: int) -> None:
            info["position"], info["pid"] = position, pid

        def paused() -> bool:
            # also keeps the paused time, so the controller's elapsed timer stands still while paused
            now = time.time()
            frozen = self.is_paused(batch.id)
            if frozen and not info.get("paused_since"):
                info["paused_since"] = now
            elif not frozen and info.get("paused_since"):
                info["paused_s"] = info.get("paused_s", 0.0) + now - info["paused_since"]
                info["paused_since"] = None
            info["paused"] = frozen
            return frozen

        try:
            if not os.path.exists(path):
                record = {"stream_id": stream["stream_id"], "index": stream["index"], "codec": stream["codec"],
                          "mode": mode.name, "seconds": 0, "status": "failed",
                          "error": f"file not found on worker {self.name}: {path} (mount the media at "
                                   f"{self.config.worker.media_root}, or set PATH_MAP)"}
            else:
                timeout = duration * self.config.worker.timeout_factor + self.config.worker.timeout_base
                record = measure_track(path, stream, duration, mode, timeout, progress, track_stop, paused,
                                       stall=self.config.worker.stall_seconds)
            if record["status"] == "stopped":
                return
            record.update(part_id=job["part_id"], file=job["file"], size=job["size"], retry=retry,
                          worker=self.name, at=time.time())
            batch.add_result(self.name, record)
            with self.lock:
                self.stats["ok" if record["status"] == "ok" else "failed"] += 1
                self.stats["audio_s"] += duration
            line = (f"I={record['ln']['ln:loudness']} TP={record['ln']['ln:peak']}" if record["status"] == "ok"
                    else record.get("error", "")[:150])
            print(f"{time.strftime('%H:%M:%S')} {record['status']:<6} {record['seconds']:>7.0f}s "
                  f"{job['title'][:55]} [{stream['codec']}] {line}", flush=True)
        finally:
            batch.release(stream["stream_id"])
            with self.lock:
                self.active.pop(key, None)
            self.track_stops.pop(key, None)

    def next_tracks(self, free: int) -> list:
        """Claim up to `free` tracks, oldest batch first (none while this worker is paused)."""
        picked = []
        if self.self_paused or self.off_hours:
            return picked
        for batch in self.queue.batches():
            if self.only_batch and not batch.id.startswith(self.only_batch):
                continue
            pending = batch.pending()
            if not pending:
                continue
            data = batch.data
            mode, retry = Mode.from_dict(data.get("mode", {})), data.get("retry", 0)
            busy = batch.live_claims()
            for job, stream in pending:
                if len(picked) >= free:
                    return picked
                if stream["stream_id"] in busy:
                    continue
                if batch.claim(stream["stream_id"], self.name):
                    picked.append((batch, job, stream, mode, retry))
        return picked

    def run(self) -> int:
        self.queue.ensure()
        print(f"plex-loudness-worker {__version__} '{self.name}': {self.slots} parallel tracks, "
              f"work dir {self.config.work_dir}, media {self.config.worker.media_root}, "
              f"path map {self.config.worker.path_map or 'auto'}", flush=True)
        threading.Thread(target=self.heartbeat, daemon=True).start()
        self.queue.write_worker(self.name, self.status())
        futures: set[concurrent.futures.Future] = set()
        idle_since = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.slots) as pool:
            while not self.stop.is_set():
                self.refresh_pauses()
                self.drop_deleted()
                futures = {f for f in futures if not f.done()}
                free = self.slots - len(futures)
                picked = self.next_tracks(free) if free else []
                for item in picked:
                    futures.add(pool.submit(self.measure, *item))
                if not futures and not picked:
                    if self.once:
                        break
                    if idle_since is None:
                        idle_since = time.time()
                        print(f"{time.strftime('%H:%M:%S')} idle, checking for new batches every "
                              f"{self.config.worker.poll_seconds:g}s", flush=True)
                    self.stop.wait(self.config.worker.poll_seconds)
                    continue
                idle_since = None
                self.stop.wait(2 if free else 5)
            if self.stop.is_set():
                print("Stopping: running ffmpeg processes are killed and their tracks released", flush=True)
        self.stop.set()
        self.queue.write_worker(self.name, self.status())
        s = self.status()
        print(f"Done: {s['ok']} ok, {s['failed']} failed, {s['audio_hours']:.1f} h of audio", flush=True)
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="plex-loudness-worker", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="config file (default: search, see README)")
    parser.add_argument("--workers", type=int, help="parallel tracks (default from config, else CPUs - 2)")
    parser.add_argument("--name", help="worker name shown in the controller (default: hostname)")
    parser.add_argument("--batch", help="only work on this batch id (prefix)")
    parser.add_argument("--once", action="store_true", help="exit when there is nothing left to do")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    config = load(args.config)
    if args.name:
        config.worker.name = args.name
    worker = Worker(config, args.workers or config.worker_count, args.batch, args.once)

    def stop(signum, _frame):
        worker.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
