"""Measuring one audio track with ffmpeg's loudnorm, the same way Plex does."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .extradata import LOUDNORM, loudness_fields

_JSON_BLOCK = re.compile(r"\[Parsed_loudnorm_(\d+) @ [^\]]+\]\s*\n(\{.*?\})", re.S)


@dataclass(frozen=True)
class Mode:
    """How to measure: the whole track (exact) or N samples of S seconds (approximate)."""
    samples: int = 0
    seconds: float = 30

    @property
    def name(self) -> str:
        if not self.samples:
            return "full"
        if (self.samples, self.seconds) == (QUICK.samples, QUICK.seconds):
            return "quick"
        return f"sampled {self.samples}x{self.seconds:g}s"

    @property
    def exact(self) -> bool:
        return not self.samples

    def as_dict(self) -> dict:
        return {"samples": self.samples, "seconds": self.seconds, "name": self.name}

    @classmethod
    def parse(cls, name: str, samples: int = 0, seconds: float = 30) -> "Mode":
        if name == "full":
            return FULL
        if name == "quick":
            return QUICK
        if name == "sampled":
            if samples < 1:
                raise ValueError("sampled mode needs --samples N")
            return cls(samples, seconds)
        raise ValueError(f"Unknown mode {name!r} (full, quick, sampled)")

    @classmethod
    def from_dict(cls, data: dict) -> "Mode":
        return cls(int(data.get("samples", 0)), float(data.get("seconds", 30)))


FULL = Mode()
QUICK = Mode(24, 30)   # the most accurate sampling preset in testing (mean loudness error ~0.7 dB)


def sample_starts(duration_s: float, count: int, length: float) -> list[float]:
    """Evenly spread sample start times, each centred in its slice of the track."""
    usable = max(duration_s - length, 0)
    return [round(usable * (i + 0.5) / count, 3) for i in range(count)]


def ffmpeg_command(path: str, stream_index: int, duration_s: float, mode: Mode):
    """Returns (argv, seconds of audio that will be measured, temp file to delete or None)."""
    cmd = ["ffmpeg", "-nostdin", "-nostats", "-hide_banner", "-loglevel", "info",
           "-progress", "pipe:1", "-stats_period", "5"]
    listing = None
    if mode.samples and duration_s > mode.samples * mode.seconds * 2:
        # One concat-demuxer input that seeks from segment to segment. (Opening the file once per
        # sample and joining them with the concat *filter* can deadlock ffmpeg's input threads.)
        quoted = path.replace("'", "'\\''")
        lines = ["ffconcat version 1.0"]
        for start in sample_starts(duration_s, mode.samples, mode.seconds):
            lines += [f"file '{quoted}'", f"inpoint {start:.3f}", f"outpoint {start + mode.seconds:.3f}"]
        handle, listing = tempfile.mkstemp(prefix="plex-loudness-", suffix=".ffconcat")
        with os.fdopen(handle, "w") as out:
            out.write("\n".join(lines) + "\n")
        cmd += ["-f", "concat", "-safe", "0", "-i", listing]
        total = mode.samples * mode.seconds
    else:
        cmd += ["-i", path]
        total = duration_s
    cmd += ["-filter_complex", f"[0:{stream_index}]{LOUDNORM}[out]", "-map", "[out]", "-f", "null", "-"]
    return cmd, total, listing


def parse_measure(stderr: str) -> dict | None:
    """The last loudnorm JSON block in ffmpeg's output."""
    blocks = []
    for number, block in _JSON_BLOCK.findall(stderr):
        try:
            blocks.append((int(number), json.loads(block)))
        except ValueError:
            pass
    return sorted(blocks, key=lambda item: item[0])[-1][1] if blocks else None


def rss_mb(pid: int | None) -> int:
    if not pid:
        return 0
    try:
        with open(f"/proc/{pid}/status") as handle:
            match = re.search(r"VmRSS:\s+(\d+)", handle.read())
        return int(match[1]) // 1024 if match else 0
    except OSError:
        return 0


def run_ffmpeg(cmd: list[str], timeout: float, on_progress: Callable[[float, int], None] | None = None,
               stop: threading.Event | None = None):
    """Run ffmpeg, reporting (seconds done, pid) from its -progress output. Returns (stderr, code)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    err_lines: list[str] = []
    reader = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    killed = {"why": None}

    def watchdog():
        while proc.poll() is None:
            if time.monotonic() > deadline:
                killed["why"] = "timeout"
            elif stop is not None and stop.is_set():
                killed["why"] = "stopped"
            if killed["why"]:
                proc.kill()
                return
            time.sleep(1)

    threading.Thread(target=watchdog, daemon=True).start()
    if on_progress:
        on_progress(0.0, proc.pid)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and on_progress:
            try:
                on_progress(int(line.split("=", 1)[1]) / 1e6, proc.pid)
            except ValueError:
                pass
    code = proc.wait()
    reader.join(5)
    return "".join(err_lines), (killed["why"] or code)


def measure_track(path: str, stream: dict, duration_s: float, mode: Mode, timeout: float,
                  on_progress=None, stop=None) -> dict:
    """Measure one audio stream. Returns a result record (status ok/failed/stopped)."""
    cmd, total, listing = ffmpeg_command(path, stream["index"], duration_s, mode)
    started = time.monotonic()
    try:
        err, code = run_ffmpeg(cmd, timeout, on_progress, stop)
    except OSError as exc:
        err, code = str(exc), "error"
    finally:
        if listing:
            try:
                os.unlink(listing)
            except OSError:
                pass
    record = {"stream_id": stream["stream_id"], "index": stream["index"], "codec": stream["codec"],
              "mode": mode.name, "seconds": round(time.monotonic() - started, 1), "measured_s": total,
              "tool": f"plex-loudness {__version__}"}
    if code == "stopped":
        return dict(record, status="stopped")
    measure = parse_measure(err)
    fields = loudness_fields(measure) if code == 0 else None
    if fields:
        return dict(record, status="ok", ln=fields)
    tail = [line for line in err.strip().splitlines() if line and not line.startswith(" ")][-3:]
    reason = "no usable measurement (silent track?)" if code == 0 else f"ffmpeg {code}"
    record.update(status="failed", error=f"{reason}: " + " | ".join(tail)[:400])
    if measure:
        record["measure"] = measure
    return record
