"""CPU and memory use of this worker (its container's cgroup) and of the host it runs on.

Read from /proc and /sys/fs/cgroup, so it works inside Docker without extra permissions:
/proc/stat, /proc/loadavg and /proc/meminfo show the whole host, the cgroup files show the
container. CPU figures are averages since the previous sample (the worker samples every heartbeat).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def host_cpu_times(proc: Path = PROC) -> tuple[int, int] | None:
    """(busy, total) jiffies from the first line of /proc/stat."""
    line = _read(proc / "stat").split("\n", 1)[0].split()
    if not line or line[0] != "cpu":
        return None
    values = [int(v) for v in line[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)      # idle + iowait
    total = sum(values[:8])                                          # guest time is already in user
    return total - idle, total


def host_memory_mb(proc: Path = PROC) -> tuple[int, int] | None:
    """(used, total) MB from /proc/meminfo, used = total - available."""
    info = {}
    for line in _read(proc / "meminfo").splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            info[key] = int(rest.split()[0])          # kB
    if "MemTotal" not in info:
        return None
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return (info["MemTotal"] - available) // 1024, info["MemTotal"] // 1024


def load_average(proc: Path = PROC) -> float | None:
    text = _read(proc / "loadavg").split()
    return float(text[0]) if text else None


def cgroup_cpu_usec(cgroup: Path = CGROUP) -> int | None:
    """Total CPU time used by this cgroup, in microseconds (v2, else v1)."""
    for line in _read(cgroup / "cpu.stat").splitlines():
        if line.startswith("usage_usec "):
            return int(line.split()[1])
    v1 = _read(cgroup / "cpuacct" / "cpuacct.usage").strip() or _read(cgroup / "cpuacct.usage").strip()
    return int(v1) // 1000 if v1.isdigit() else None


def cgroup_cpu_limit(cgroup: Path = CGROUP) -> float | None:
    """CPU limit in cores (docker --cpus), or None if unlimited."""
    text = _read(cgroup / "cpu.max").split()
    if len(text) == 2 and text[0] != "max":
        return int(text[0]) / int(text[1])
    quota = _read(cgroup / "cpu" / "cpu.cfs_quota_us").strip()
    period = _read(cgroup / "cpu" / "cpu.cfs_period_us").strip()
    if quota.lstrip("-").isdigit() and period.isdigit() and int(quota) > 0:
        return int(quota) / int(period)
    return None


def cgroup_memory_mb(cgroup: Path = CGROUP) -> tuple[int | None, int | None]:
    """(used, limit) MB for this cgroup; limit None if unlimited. Page cache isn't counted as used."""
    def mb(text: str) -> int | None:
        text = text.strip()
        return int(text) // 1048576 if text.isdigit() else None

    used, limit = mb(_read(cgroup / "memory.current")), mb(_read(cgroup / "memory.max"))
    cache = 0
    for line in _read(cgroup / "memory.stat").splitlines():
        if line.startswith(("inactive_file ", "total_inactive_file ")):
            cache = int(line.split()[1]) // 1048576
    if used is None:                                         # cgroup v1
        used = mb(_read(cgroup / "memory" / "memory.usage_in_bytes"))
        limit = mb(_read(cgroup / "memory" / "memory.limit_in_bytes"))
        if limit is not None and limit > 1 << 40:            # v1 "unlimited" is a huge number
            limit = None
    if used is not None:
        used = max(0, used - cache)
    return used, limit


class Sampler:
    """Call sample() periodically; CPU percentages are averaged since the previous call."""

    def __init__(self, proc: Path = PROC, cgroup: Path = CGROUP):
        self.proc, self.cgroup = proc, cgroup
        self.prev_host = host_cpu_times(proc)
        self.prev_cg = (cgroup_cpu_usec(cgroup), time.monotonic())

    def sample(self) -> dict:
        out: dict = {"host_cpus": os.cpu_count() or 0, "host_load": load_average(self.proc)}
        now_host = host_cpu_times(self.proc)
        if now_host and self.prev_host and now_host[1] > self.prev_host[1]:
            busy = now_host[0] - self.prev_host[0]
            out["host_cpu_pct"] = round(100 * busy / (now_host[1] - self.prev_host[1]), 1)
        self.prev_host = now_host or self.prev_host
        memory = host_memory_mb(self.proc)
        if memory:
            out["host_mem_mb"], out["host_mem_total_mb"] = memory
        usec, at = cgroup_cpu_usec(self.cgroup), time.monotonic()
        prev_usec, prev_at = self.prev_cg
        if usec is not None and prev_usec is not None and at > prev_at:
            out["cpu_cores"] = round((usec - prev_usec) / 1e6 / (at - prev_at), 2)
        self.prev_cg = (usec, at)
        out["cpu_limit"] = cgroup_cpu_limit(self.cgroup)
        out["mem_mb"], out["mem_limit_mb"] = cgroup_memory_mb(self.cgroup)
        return out


def describe(res: dict) -> tuple[str, str, str]:
    """Short (cpu, mem, host) strings for the TUI and CLI."""
    if not res:
        return "—", "—", "—"
    limit = res.get("cpu_limit") or res.get("host_cpus")
    cpu = f"{res['cpu_cores']:.1f}/{limit:g} cores" if res.get("cpu_cores") is not None and limit else "—"
    if res.get("mem_mb") is not None:
        cap = res.get("mem_limit_mb")
        mem = f"{res['mem_mb'] / 1024:.1f}/{cap / 1024:.1f} GB" if cap else f"{res['mem_mb'] / 1024:.1f} GB"
    else:
        mem = "—"
    bits = []
    if res.get("host_cpu_pct") is not None:
        bits.append(f"{res['host_cpu_pct']:.0f}% cpu")
    if res.get("host_load") is not None and res.get("host_cpus"):
        bits.append(f"load {res['host_load']:.0f}/{res['host_cpus']}")
    if res.get("host_mem_total_mb"):
        bits.append(f"{100 * res['host_mem_mb'] / res['host_mem_total_mb']:.0f}% mem")
    return cpu, mem, " · ".join(bits) or "—"


def overloaded(res: dict) -> bool:
    """The host has more runnable work than cores: every track slows down (more slots won't help)."""
    return bool(res.get("host_load") and res.get("host_cpus") and res["host_load"] > 1.2 * res["host_cpus"])
