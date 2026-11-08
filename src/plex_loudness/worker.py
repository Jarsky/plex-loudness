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


def map_path(path: str, mapping: dict[str, str]) -> str:
    for src in sorted(mapping, key=len, reverse=True):
        if path == src or path.startswith(src.rstrip("/") + "/"):
            return mapping[src].rstrip("/") + path[len(src.rstrip("/")):]
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
        self.stats = {"ok": 0, "failed": 0, "measured_s": 0.0, "audio_s": 0.0}
        self.started = time.time()

    # ---- status
    def status(self) -> dict:
        with self.lock:
            active = [dict(a, rss_mb=rss_mb(a.get("pid"))) for a in self.active.values()]
            stats = dict(self.stats)
        elapsed = time.time() - self.started
        return {"version": __version__, "slots": self.slots, "started": self.started, "elapsed": elapsed,
                "active": sorted(active, key=lambda a: a["started"]), "stopping": self.stop.is_set(),
                "ok": stats["ok"], "failed": stats["failed"], "audio_hours": stats["audio_s"] / 3600,
                "rate": stats["audio_s"] / elapsed if elapsed > 60 else 0}

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
        path = map_path(job["file"], self.config.worker.path_map)
        duration = (job.get("duration_ms") or 0) / 1000
        info = {"batch": batch.id, "batch_path": str(batch.path), "stream_id": stream["stream_id"],
                "title": job["title"], "codec": stream["codec"], "started": time.time(),
                "position": 0.0, "total": duration if mode.exact else mode.samples * mode.seconds, "pid": None}
        with self.lock:
            self.active[key] = info

        def progress(position: float, pid: int) -> None:
            info["position"], info["pid"] = position, pid

        try:
            if not os.path.exists(path):
                record = {"stream_id": stream["stream_id"], "index": stream["index"], "codec": stream["codec"],
                          "mode": mode.name, "seconds": 0, "status": "failed",
                          "error": f"file not found on worker {self.name}: {path} (check [worker.path_map])"}
            else:
                timeout = duration * self.config.worker.timeout_factor + self.config.worker.timeout_base
                record = measure_track(path, stream, duration, mode, timeout, progress, self.stop)
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

    def next_tracks(self, free: int) -> list:
        """Claim up to `free` tracks, oldest batch first."""
        picked = []
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
              f"work dir {self.config.work_dir}, path map {self.config.worker.path_map or 'none'}", flush=True)
        threading.Thread(target=self.heartbeat, daemon=True).start()
        self.queue.write_worker(self.name, self.status())
        futures: set[concurrent.futures.Future] = set()
        idle_since = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.slots) as pool:
            while not self.stop.is_set():
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
