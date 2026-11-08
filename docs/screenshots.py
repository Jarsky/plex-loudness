"""Regenerate the README banner and screenshots (SVG) from demo data:  python docs/screenshots.py

Nothing touches a real Plex server or library: libraries, batches and workers are fictional and
the titles are Blender Studio open movies. Needs the TUI extra (pip install -e '.[tui]').
"""

from __future__ import annotations

import asyncio
import io
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from plex_loudness import __version__, tui  # noqa: E402
from plex_loudness.analysis import FULL, QUICK  # noqa: E402
from plex_loudness.batches import Queue  # noqa: E402
from plex_loudness.config import Config  # noqa: E402
from plex_loudness.importer import ImportPlan  # noqa: E402
from plex_loudness.plexdb import PlexDB  # noqa: E402

OUT = Path(__file__).parent / "screenshots"
FILMS = ["Big Buck Bunny (2008)", "Sintel (2010)", "Tears of Steel (2012)", "Cosmos Laundromat (2015)",
         "Spring (2019)", "Agent 327: Operation Barbershop (2017)", "Elephants Dream (2006)", "Caminandes (2013)",
         "Sprite Fright (2021)", "Coffee Run (2020)", "Charge (2022)", "Hero (2018)", "Glass Half (2015)",
         "The Daily Dweebs (2017)", "Wing It! (2023)", "Spring (2019)"]
SHOW = "Blender Open Movie Night"
CODECS = ["eac3", "ac3", "truehd", "dca", "aac", "eac3", "ac3", "flac"]
LIBRARIES = [
    {"id": 9, "name": "Documentary (Features)", "type": 1, "items": 87, "done": 87},
    {"id": 7, "name": "Documentary (Series)", "type": 2, "items": 409, "done": 211},
    {"id": 2, "name": "Movies", "type": 1, "items": 4802, "done": 3964},
    {"id": 11, "name": "Music Videos", "type": 1, "items": 925, "done": 925},
    {"id": 1, "name": "TV Shows", "type": 2, "items": 33303, "done": 9431},
]


def job(i: int, tv: bool, streams: int = 1) -> dict:
    title = f"{SHOW} S{i // 20 + 1:02d}E{i % 20 + 1:02d}" if tv else FILMS[i % len(FILMS)]
    return {"part_id": 10_000 + i, "item_id": 20_000 + i, "file": f"/{'tv' if tv else 'movies'}/{title}/{i}.mkv",
            "size": 1_000_000 + i, "duration_ms": (44 if tv else 105) * 60_000, "library": "TV Shows" if tv else "Movies",
            "title": title, "added_at": 0, "year": 2020,
            "streams": [{"stream_id": 100_000 + i * 4 + s, "index": s + 1, "codec": CODECS[(i + s) % len(CODECS)],
                         "channels": 6, "language": "eng"} for s in range(streams)]}


def ok(stream: dict, job_: dict, rng: random.Random) -> dict:
    loud = round(rng.uniform(-28, -14), 2)
    return {"stream_id": stream["stream_id"], "index": stream["index"], "codec": stream["codec"], "mode": "full",
            "status": "ok", "seconds": 300, "part_id": job_["part_id"], "file": job_["file"], "size": job_["size"],
            "retry": 0, "at": time.time(), "ln": {"ln:loudness": f"{loud:.2f}", "ln:peak": "-1.20", "ln:lra": "12.00",
                                                   "ln:threshold": "-35.00", "ln:gainOffset": "0.50",
                                                   "ln:loudnessAnalysisVersion": "0.02"}}


def demo_work_dir(root: Path) -> None:
    rng = random.Random(7)
    queue = Queue(root)
    mv = queue.create("Music Videos", [job(i, False) for i in range(925)], FULL.as_dict(), "everything not yet analysed", "demo")
    for j in mv.data["jobs"]:
        mv.add_result("nas", ok(j["streams"][0], j, rng))
    mv.mark_imported({"written": 925, "backup": "(demo)"})
    time.sleep(1.1)
    tv_jobs = [job(i, True) for i in range(4212)]
    tv = queue.create("TV Shows", tv_jobs, FULL.as_dict(), "added since 2025-11-08", "demo")
    for n, j in enumerate(tv_jobs[:1874]):
        rec = ok(j["streams"][0], j, rng)
        if n in (77, 640, 1501):
            rec.update(status="failed", error="no usable measurement (silent track?)")
            rec.pop("ln")
        tv.add_result("nas" if n % 3 else "desktop", rec)
    time.sleep(1.1)
    queue.create("Movies", [job(i, False, streams=1 + i % 3) for i in range(370)], QUICK.as_dict(),
                 "year 1990-1999", "demo")
    active = {"nas": [], "desktop": []}
    for k, j in enumerate(tv_jobs[1874:1874 + 20]):
        tv.claim(j["streams"][0]["stream_id"], "nas")
        active["nas"].append({"batch": tv.id, "stream_id": j["streams"][0]["stream_id"], "title": FILMS[k % len(FILMS)]
                              if k % 4 == 0 else j["title"], "codec": j["streams"][0]["codec"],
                              "started": time.time() - rng.uniform(30, 900), "position": 0, "total": 2640,
                              "rss_mb": rng.randint(140, 320)})
    for j in tv_jobs[1894:1894 + 16]:
        tv.claim(j["streams"][0]["stream_id"], "desktop")
        active["desktop"].append({"batch": tv.id, "stream_id": j["streams"][0]["stream_id"], "title": j["title"],
                                  "codec": j["streams"][0]["codec"], "started": time.time() - rng.uniform(20, 600),
                                  "position": 0, "total": 2640, "rss_mb": rng.randint(140, 260)})
    for a in active["nas"] + active["desktop"]:
        a["position"] = min(a["total"] * 0.98, (time.time() - a["started"]) * rng.uniform(1.5, 5))
    queue.write_worker("nas", {"version": __version__, "slots": 20, "active": active["nas"], "ok": 1247, "failed": 2,
                               "audio_hours": 914.0, "rate": 52, "started": time.time() - 64_000})
    queue.write_worker("desktop", {"version": __version__, "slots": 16, "active": active["desktop"], "ok": 627,
                                   "failed": 1, "audio_hours": 460.0, "rate": 41, "started": time.time() - 40_000})


def patch_tui() -> None:
    tui.libraries = lambda db: LIBRARIES
    tui.preview = lambda db, flt: {"files": 3_212, "tracks": 3_480, "hours": 2_361.0}
    PlexDB.describe = lambda self: "container plex as abc"

    def fake_plan(db, records, *args, **kwargs):
        good = [r for r in records if r.get("status") == "ok"]
        plan = ImportPlan(write=len(good), failed=len(records) - len(good))
        plan.statements = ["-- demo"] * len(good)
        return plan

    tui.plan_import = fake_plan


async def shoot(work: Path) -> None:
    config = Config(work_dir=str(work))
    app = tui.LoudnessApp(config)
    app.config = Config(work_dir="/work")   # what the subtitle shows; the queue already points at the demo dir
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause(1.5)
        libs = app.query_one("#libs")
        libs.select(1)
        app.query_one("#since").value = "1y"
        await pilot.press("p")
        await pilot.pause(1.0)
        table = app.query_one("#batches")
        table.move_cursor(row=1)
        await pilot.pause(3.5)
        (OUT / "tui.svg").write_text(app.export_screenshot(title="plex-loudness"))
        print("wrote tui.svg")


def banner_svg() -> None:
    console = Console(record=True, width=120, force_terminal=True, color_system="truecolor", file=io.StringIO())
    console.print(tui.banner(120, 60))
    console.print(Text("fill in Plex's loudness data with ffmpeg · in parallel · on any machine",
                       style="grey62", justify="center"))
    console.save_svg(str(Path(__file__).parent / "banner.svg"), title="plex-loudness")
    print("wrote banner.svg")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    banner_svg()
    with tempfile.TemporaryDirectory() as tmp:
        demo_work_dir(Path(tmp))
        patch_tui()
        asyncio.run(shoot(Path(tmp)))


if __name__ == "__main__":
    main()
