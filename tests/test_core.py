import json
import time

import pytest

from plex_loudness.analysis import FULL, QUICK, Mode, ffmpeg_command, parse_measure, sample_starts
from plex_loudness.batches import Queue
from plex_loudness.catalog import Filter, _where, parse_when, parse_years
from plex_loudness.extradata import build_extra, loudness_fields, url_encode
from plex_loudness.worker import map_path

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
        text = open(listing).read()
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
    assert "IN (1,2)" in sql and ">= 1990" in sql and "added_at >= 100" in sql and "NOT (" in sql


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
