"""Comparing measurements: a sampled run against a full one, or anything against Plex's own values."""

from __future__ import annotations

import json

from .catalog import stream_extra
from .extradata import COMPARE_KEYS
from .plexdb import PlexDB


def plex_reference(db: PlexDB, stream_ids) -> dict[int, dict]:
    rows = stream_extra(db, stream_ids)
    out = {}
    for sid, row in rows.items():
        fields = json.loads(row["extra_data"] or "{}")
        if "ln:loudness" in fields:
            out[sid] = {"ln": fields, "seconds": None}
    return out


def compare(test: dict[int, dict], reference: dict[int, dict], titles: dict[int, str] | None = None) -> dict:
    """Per-track differences (test - reference) and summary statistics per field."""
    test = {k: v for k, v in test.items() if v.get("status", "ok") == "ok"}
    reference = {k: v for k, v in reference.items() if v.get("status", "ok") == "ok"}
    common = sorted(sid for sid in test if sid in reference)
    rows, errors = [], {k: [] for k in COMPARE_KEYS}
    for sid in common:
        a, b = reference[sid]["ln"], test[sid]["ln"]
        diffs = {k: float(b[k]) - float(a[k]) for k in COMPARE_KEYS}
        for k, d in diffs.items():
            errors[k].append(abs(d))
        name = (titles or {}).get(sid) or test[sid].get("file", str(sid)).rsplit("/", 2)[-2]
        rows.append({"stream_id": sid, "title": name, "codec": test[sid].get("codec", ""),
                     "reference": {k: a[k] for k in COMPARE_KEYS}, "diff": diffs})
    summary = {}
    for k, e in errors.items():
        if e:
            summary[k] = {"exact": sum(1 for x in e if x < 0.005), "mean": sum(e) / len(e), "max": max(e),
                          "within_1db": sum(1 for x in e if x <= 1.0), "n": len(e)}
    t_secs = sum(test[s].get("seconds") or 0 for s in common)
    r_secs = sum(reference[s].get("seconds") or 0 for s in common)
    return {"tracks": len(common), "rows": sorted(rows, key=lambda r: (r["title"], r["codec"], r["stream_id"])),
            "summary": summary, "seconds": t_secs, "reference_seconds": r_secs or None}


def format_report(result: dict, label: str, reference_label: str) -> str:
    lines = [f"{label} vs {reference_label}: {result['tracks']} audio tracks", ""]
    head = "".join(f"{k[3:]:>17}" for k in COMPARE_KEYS)
    lines.append(f"{'title':<38}{'codec':<7}{head}")
    for row in result["rows"]:
        cells = "".join(f"{row['reference'][k]:>9}{row['diff'][k]:>+8.2f}" for k in COMPARE_KEYS)
        lines.append(f"{row['title'][:37]:<38}{row['codec'][:6]:<7}{cells}")
    lines += ["", "(each column: reference value, then the difference)", "",
              f"{'field':<14}{'exact':>8}{'mean err':>10}{'max err':>9}{'within 1 dB':>13}"]
    for k, s in result["summary"].items():
        lines.append(f"{k[3:]:<14}{s['exact']:>5}/{s['n']:<2}{s['mean']:>10.2f}{s['max']:>9.2f}"
                     f"{s['within_1db']:>10}/{s['n']}")
    if result["reference_seconds"]:
        speed = result["reference_seconds"] / max(result["seconds"], 0.1)
        lines.append(f"\nCPU time: {result['seconds'] / 60:,.1f} min vs {result['reference_seconds'] / 60:,.1f} min"
                     f" ({speed:,.1f}x faster)")
    return "\n".join(lines)
