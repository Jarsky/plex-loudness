import json
import time

import pytest

from plex_loudness.analysis import FULL, QUICK, Mode, ffmpeg_command, parse_measure, run_ffmpeg, sample_starts
from plex_loudness.batches import Queue, run_seconds
from plex_loudness.catalog import Filter, _where, parse_when, parse_years, preview, select_jobs
from plex_loudness.config import Config
from plex_loudness.extradata import build_extra, loudness_fields, url_encode
from plex_loudness.worker import TrackStop, Worker, map_path

PLEX_ROW = ('{"ln:gainOffset":"0.97","ln:loudness":"-22.88","ln:loudnessAnalysisVersion":"0.02","ln:lra":"21.20",'
            '"ln:peak":"-2.46","ln:threshold":"-35.22","ma:audioChannelLayout":"5.1(side)",'
            '"ma:requiredBandwidths":"640,640,640,640,640,640,640,640","ma:samplingRate":"48000","ma:title":"Surround",'
            '"url":"ln%3AgainOffset=0%2E97&ln%3Aloudness=-22%2E88&ln%3AloudnessAnalysisVersion=0%2E02&ln%3Alra=21%2E20'
            '&ln%3Apeak=-2%2E46&ln%3Athreshold=-35%2E22&ma%3AaudioChannelLayout=5%2E1%28side%29'
            '&ma%3ArequiredBandwidths=640%2C640%2C640%2C640%2C640%2C640%2C640%2C640&ma%3AsamplingRate=48000'
            '&ma%3Atitle=Surround"}')


def test_rebuilds_plex_row_exactly():
    assert build_extra(json.loads(PLEX_ROW), like=PLEX_ROW) == PLEX_ROW


@pytest.mark.parametrize("escaped", [True, False])
def test_keeps_the_rows_unicode_style(escaped):
    title = "Español (Latinoamericano)"
    fields = {"ma:title": title}
    url = "ma%3Atitle=Espa%C3%B1ol%20%28Latinoamericano%29"
    body = json.dumps({"ma:title": title, "url": url}, separators=(",", ":"), ensure_ascii=escaped)
    assert build_extra(fields, like=body) == body


def test_url_encoding_keeps_tilde_dash_underscore():
    assert url_encode("DTS-HD ~4200 kbps_x.y") == "DTS-HD%20~4200%20kbps_x%2Ey"


def test_loudness_fields_rejects_silence():
    good = {"input_i": "-22.88", "input_tp": "-2.46", "input_lra": "21.20", "input_thresh": "-35.22",
            "target_offset": "0.97"}
    assert loudness_fields(good)["ln:loudness"] == "-22.88"
    assert loudness_fields(dict(good, input_i="-inf")) is None


def test_parse_measure_takes_loudnorm_block():
    err = "junk\n[Parsed_loudnorm_1 @ 0x55]\n{\n\t\"input_i\" : \"-20.00\"\n}\nmore"
    assert parse_measure(err) == {"input_i": "-20.00"}


def test_sampling_uses_one_concat_input(tmp_path):
    cmd, total, listing = ffmpeg_command("/m/It's a film.mkv", 2, 6000, QUICK)
    try:
        assert cmd.count("-i") == 1 and "concat" in cmd and total == 24 * 30
        with open(listing) as handle:
            text = handle.read()
        assert text.count("inpoint") == 24 and "It'\\''s a film" in text
    finally:
        import os
        os.unlink(listing)


def test_full_mode_and_short_files_read_everything():
    cmd, total, listing = ffmpeg_command("/m/a.mkv", 1, 600, QUICK)  # too short to sample
    assert listing is None and total == 600 and "[0:1]" in cmd[cmd.index("-filter_complex") + 1]


def test_sample_starts_are_spread_and_inside():
    starts = sample_starts(1000, 4, 30)
    assert starts[0] > 0 and starts[-1] + 30 <= 1000 and starts == sorted(starts)


def test_mode_names():
    assert FULL.name == "full" and QUICK.name == "quick" and Mode(5, 10).name == "sampled 5x10s"
    assert Mode.from_dict(QUICK.as_dict()) == QUICK


def test_dates_and_years():
    assert abs(parse_when("30d") - (time.time() - 30 * 86400)) < 5
    assert parse_when("") is None
    assert parse_years("1990-1999") == (1990, 1999)
    assert parse_years("2001") == (2001, 2001)
    assert parse_years("2010-") == (2010, None)
    with pytest.raises(ValueError):
        parse_when("last tuesday")


def test_filter_sql():
    sql = _where(Filter(sections=[1, 2], year_from=1990, year_to=1999, added_since=100))
    assert "IN (1,2)" in sql and ">= 1990" in sql and "NOT (" in sql
    assert "MAX(md.added_at, COALESCE(ms.created_at, 0)) >= 100" in sql   # replaced files count as new


def test_path_map_prefers_longest_prefix():
    mapping = {"/tv": "/media/tv", "/tv/kids": "/kids"}
    assert map_path("/tv/kids/a.mkv", mapping) == "/kids/a.mkv"
    assert map_path("/tv/show/a.mkv", mapping) == "/media/tv/show/a.mkv"
    assert map_path("/tvx/a.mkv", mapping) == "/tvx/a.mkv"


def test_queue_claims_results_and_retry(tmp_path):
    queue = Queue(tmp_path)
    job = {"part_id": 1, "file": "/m/a.mkv", "size": 10, "duration_ms": 60000, "title": "A",
           "streams": [{"stream_id": 7, "index": 1, "codec": "ac3"}, {"stream_id": 8, "index": 2, "codec": "dca"}]}
    batch = queue.create("Movies", [job], FULL.as_dict(), "test", "host")
    assert len(batch.pending()) == 2
    assert batch.claim(7, "w1") and not batch.claim(7, "w2")
    batch.add_result("w1", {"stream_id": 7, "status": "ok", "ln": {}, "retry": 0, "at": 1})
    batch.add_result("w2", {"stream_id": 8, "status": "failed", "retry": 0, "at": 2})
    batch.release(7)
    assert batch.pending() == []
    assert batch.summary()["state"] == "finished"
    assert batch.retry_failed() == 1
    assert [s["stream_id"] for _, s in batch.pending()] == [8]
    batch.cancel()
    assert batch.pending() == []
    assert queue.get("movies").id == batch.id and queue.get(batch.id[:8]).id == batch.id   # by name or id prefix


def test_path_resolver_finds_plex_roots_under_media(tmp_path):
    from plex_loudness.worker import PathResolver
    media = tmp_path / "media"
    (media / "tv_shows" / "Show" / "Season 1").mkdir(parents=True)
    (media / "tv_shows" / "Show" / "Season 1" / "e1.mkv").write_text("x")
    (media / "movies" / "Film (2001)").mkdir(parents=True)
    (media / "movies" / "Film (2001)" / "f.mkv").write_text("x")
    seen = []
    resolver = PathResolver({}, str(media), log=seen.append)
    assert resolver.resolve("/tv/Show/Season 1/e1.mkv") == str(media / "tv_shows/Show/Season 1/e1.mkv")
    assert resolver.resolve("/movies/Film (2001)/f.mkv") == str(media / "movies/Film (2001)/f.mkv")
    assert resolver.mapping["/tv"] == str(media / "tv_shows") and len(seen) == 2
    # remembered: a file that doesn't exist yet still maps through the learned root
    assert resolver.resolve("/tv/Other/x.mkv") == str(media / "tv_shows/Other/x.mkv")
    assert resolver.resolve("/nowhere/a.mkv") == "/nowhere/a.mkv"


def test_parse_path_map():
    from plex_loudness.config import parse_path_map
    assert parse_path_map("/tv=/media/tv_shows, /movies=/media/movies") == {"/tv": "/media/tv_shows",
                                                                            "/movies": "/media/movies"}
    assert parse_path_map("") == {}


def test_env_overrides(monkeypatch):
    from plex_loudness.config import load
    monkeypatch.setenv("WORKERS", "12")
    monkeypatch.setenv("PATH_MAP", "/tv=/media/tv")
    monkeypatch.setenv("PLEX_CONTAINER", "myplex")
    config = load()
    assert config.worker.workers == 12 and config.worker.path_map == {"/tv": "/media/tv"}
    assert config.plex.container == "myplex"


# --------------------------------------------------------------------------- added since (replaced files)

class FakeDB:
    """Just enough of PlexDB for catalog queries: an in-memory SQLite with Plex's table shapes."""
    def __init__(self):
        import sqlite3
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE library_sections (id INTEGER, name TEXT, section_type INTEGER);
            CREATE TABLE metadata_items (id INTEGER, library_section_id INTEGER, metadata_type INTEGER, parent_id INTEGER,
                                         title TEXT, year INTEGER, "index" INTEGER, added_at INTEGER);
            CREATE TABLE media_items (id INTEGER, metadata_item_id INTEGER, duration INTEGER, deleted_at INTEGER);
            CREATE TABLE media_parts (id INTEGER, media_item_id INTEGER, file TEXT, size INTEGER, deleted_at INTEGER);
            CREATE TABLE media_streams (id INTEGER, media_part_id INTEGER, stream_type_id INTEGER, "index" INTEGER,
                                        codec TEXT, channels INTEGER, language TEXT, extra_data TEXT, created_at INTEGER);
            INSERT INTO library_sections VALUES (1, 'Movies', 1);
        """)

    def add_movie(self, n, added_at, stream_created_at, extra=""):
        self.con.execute("INSERT INTO metadata_items VALUES (?,1,1,NULL,?,2000,NULL,?)", (n, f"Movie {n}", added_at))
        self.con.execute("INSERT INTO media_items VALUES (?,?,7200000,NULL)", (n, n))
        self.con.execute("INSERT INTO media_parts VALUES (?,?,?,1,NULL)", (n, n, f"/movies/{n}.mkv"))
        self.con.execute("INSERT INTO media_streams VALUES (?,?,2,1,'eac3',6,'eng',?,?)",
                         (n, n, extra, stream_created_at))

    def query(self, sql):
        return [dict(r) for r in self.con.execute(sql)]


def test_added_since_includes_replaced_files():
    now = int(time.time())
    old, recent = now - 400 * 86400, now - 2 * 86400
    db = FakeDB()
    db.add_movie(1, added_at=recent, stream_created_at=recent)      # new movie
    db.add_movie(2, added_at=old, stream_created_at=recent)         # old movie, file replaced this week
    db.add_movie(3, added_at=old, stream_created_at=old)            # old movie, old file
    db.add_movie(4, added_at=old, stream_created_at=None)           # old row without stream timestamps
    flt = Filter(added_since=now - 30 * 86400)
    assert sorted(j["item_id"] for j in select_jobs(db, flt)) == [1, 2]
    assert preview(db, flt)["files"] == 2
    assert sorted(j["item_id"] for j in select_jobs(db, Filter(added_before=now - 30 * 86400))) == [3, 4]
    assert "added or replaced since" in flt.describe()


# --------------------------------------------------------------------------- pause / resume

def test_paused_ffmpeg_is_frozen_and_the_pause_doesnt_count_towards_the_timeout(tmp_path):
    started = time.monotonic()
    # 1.5 s of work with a 1.2 s timeout: only finishes because the 1 s pause isn't counted
    script = tmp_path / "work.sh"
    script.write_text("#!/bin/sh\nsleep 1.5\n")
    script.chmod(0o755)
    err, code, paused_s = run_ffmpeg([str(script)], timeout=2.2, poll=0.05,
                                     paused=lambda: time.monotonic() - started < 1.0)
    assert code == 0
    assert 0.8 < paused_s < 1.5
    # not paused: a too-short timeout still kills it
    _, code, paused_s = run_ffmpeg([str(script)], timeout=0.3, poll=0.05)
    assert code == "timeout" and paused_s == 0


def test_stop_kills_a_frozen_process(tmp_path):
    import threading
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    _, code, _ = run_ffmpeg(["sleep", "30"], timeout=60, poll=0.05, stop=stop, paused=lambda: True)
    assert code == "stopped"


def test_batch_pause_resume_and_cancel(tmp_path):
    queue = Queue(tmp_path)
    job = {"part_id": 1, "file": "/m/a.mkv", "size": 10, "duration_ms": 60000, "title": "A",
           "streams": [{"stream_id": 7, "index": 1, "codec": "ac3"}]}
    batch = queue.create("Movies", [job], FULL.as_dict(), "test", "host")
    batch.pause()
    assert batch.paused and batch.pending() == [] and batch.summary()["state"] == "paused"
    batch.resume()
    assert not batch.paused and len(batch.pending()) == 1 and batch.summary()["state"] == "queued"
    batch.pause()
    batch.cancel()                      # cancelling wakes frozen tracks so they can finish
    assert not batch.paused and batch.cancelled
    batch.resume()
    assert not batch.cancelled and len(batch.pending()) == 1


def test_worker_pause_markers(tmp_path):
    queue = Queue(tmp_path)
    queue.write_worker("NAS Box", {"slots": 4, "active": []})
    assert not queue.worker_paused("NAS Box")
    queue.pause_worker(queue.get_worker("NAS")["name"])     # unique prefix works
    assert queue.worker_paused("NAS Box") and queue.workers()[0]["pause_requested"]
    queue.resume_worker("NAS Box")
    assert not queue.worker_paused("NAS Box")
    with pytest.raises(SystemExit):
        queue.get_worker("desktop")


def test_paused_workers_are_listed_so_a_batch_resume_can_warn(tmp_path):
    queue = Queue(tmp_path)
    queue.write_worker("nas", {"slots": 4, "active": []})
    queue.write_worker("desktop", {"slots": 4, "active": []})
    assert queue.paused_workers() == []
    queue.pause_worker("nas")
    assert queue.paused_workers() == ["nas"]


def test_elapsed_time_of_a_track_leaves_out_its_pauses():
    now = 10_000.0
    assert run_seconds({"started": now - 100}, now) == 100                       # older workers: no pause info
    assert run_seconds({"started": now - 100, "paused_s": 30, "paused_since": None}, now) == 70
    # paused right now for 20 s (after an earlier 30 s pause): the timer stands still
    assert run_seconds({"started": now - 100, "paused_s": 30, "paused_since": now - 20}, now) == 50
    assert run_seconds({"started": now - 100, "paused_s": 30, "paused_since": now - 20}, now + 60) == 50


def test_stalled_ffmpeg_is_killed_but_a_slow_one_is_not(tmp_path):
    # repeats the same position (stuck) -> stalled
    stuck = tmp_path / "stuck.sh"
    stuck.write_text("#!/bin/sh\nwhile true; do echo out_time_us=1000000; sleep 0.1; done\n")
    stuck.chmod(0o755)
    _, code, _ = run_ffmpeg([str(stuck)], timeout=60, poll=0.05, stall=0.6)
    assert code == "stalled"
    # slow but moving -> left alone and finishes
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nfor i in 1 2 3 4 5 6 7 8; do echo out_time_us=${i}000000; sleep 0.2; done\n")
    slow.chmod(0o755)
    _, code, _ = run_ffmpeg([str(slow)], timeout=60, poll=0.05, stall=0.6)
    assert code == 0


# --------------------------------------------------------------------------- worker resources

def test_resource_sampler_reads_proc_and_cgroup(tmp_path):
    from plex_loudness.resources import Sampler, describe, overloaded
    proc, cg = tmp_path / "proc", tmp_path / "cg"
    proc.mkdir()
    cg.mkdir()
    (proc / "loadavg").write_text("59.1 58.0 57.0 3/1000 123\n")
    (proc / "meminfo").write_text("MemTotal:       65536000 kB\nMemFree: 1000 kB\nMemAvailable:   16384000 kB\n")
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (cg / "cpu.stat").write_text("usage_usec 1000000\n")
    (cg / "cpu.max").write_text("2000000 100000\n")             # --cpus 20
    (cg / "memory.current").write_text(str(5 * 1024 ** 3) + "\n")
    (cg / "memory.max").write_text(str(6 * 1024 ** 3) + "\n")
    (cg / "memory.stat").write_text("inactive_file " + str(1024 ** 3) + "\n")
    sampler = Sampler(proc, cg)
    (proc / "stat").write_text("cpu  190 0 190 820 0 0 0 0 0 0\n")     # 180 busy of 200 -> 90%
    (cg / "cpu.stat").write_text("usage_usec 3000000\n")
    res = sampler.sample()
    assert res["host_cpu_pct"] == 90.0 and res["cpu_limit"] == 20 and res["mem_mb"] == 4096
    assert res["host_mem_mb"] == 48000 and res["cpu_cores"] > 0
    cpu, mem, host = describe(dict(res, host_cpus=32))
    assert cpu.endswith("/20 cores") and mem == "4.0/6.0 GB" and "load 59/32" in host and "75% mem" in host
    assert overloaded(dict(res, host_cpus=32)) and not overloaded(dict(res, host_load=10, host_cpus=32))
    assert describe({}) == ("—", "—", "—")



def test_tracks_of_a_deleted_batch_are_dropped_and_leave_no_folder_behind(tmp_path):
    config = Config()
    config.work_dir = str(tmp_path)
    queue = Queue(tmp_path)
    batch = queue.create("movies", [{"title": "x", "file": "/x", "duration_ms": 1000, "part_id": 1, "size": 1,
                                     "streams": [{"stream_id": 1, "index": 1, "codec": "aac"}]}],
                         FULL.as_dict(), "", "test")
    worker = Worker(config, slots=1)
    key = (batch.id, 1)
    worker.active[key] = {"batch": batch.id, "batch_path": str(batch.path), "title": "x"}
    worker.track_stops[key] = stop = TrackStop(worker.stop)
    worker.drop_deleted()
    assert not stop.is_set()
    queue.delete(batch)
    worker.drop_deleted()
    assert not stop.is_set()            # one miss could be a share hiccup
    worker.drop_deleted()
    assert stop.is_set() and not worker.stop.is_set()
    batch.add_result("nas", {"stream_id": 1, "status": "ok"})
    assert not batch.path.exists()


def test_worker_outside_the_schedule_takes_nothing_and_freezes_its_tracks(tmp_path):
    from plex_loudness.schedule import Schedule, save_schedule
    config = Config()
    config.work_dir = str(tmp_path)
    queue = Queue(tmp_path)
    queue.create("movies", [{"title": "x", "file": "/x", "duration_ms": 1000, "part_id": 1, "size": 1,
                             "streams": [{"stream_id": 1, "index": 1, "codec": "aac"}]}], FULL.as_dict(), "", "t")
    worker = Worker(config, slots=1)
    save_schedule(tmp_path, Schedule(enabled=True, days=[], start="00:00", end="00:00"))   # never open
    worker.refresh_pauses()
    assert worker.off_hours and worker.is_paused("any") and worker.next_tracks(1) == []
    assert worker.status()["off_hours"]
    save_schedule(tmp_path, Schedule(enabled=False))
    worker.refresh_pauses()
    assert not worker.off_hours and len(worker.next_tracks(1)) == 1
