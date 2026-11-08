"""Plex's loudness fields and the exact way Plex serialises media_streams.extra_data.

Plex runs ffmpeg's loudnorm filter with these settings and stores, per audio stream:

    ln:loudness  = input_i         ln:peak      = input_tp
    ln:lra       = input_lra       ln:threshold = input_thresh
    ln:gainOffset = target_offset  ln:loudnessAnalysisVersion = "0.02"

extra_data is compact JSON with sorted keys, plus a "url" key holding the same fields
URL-encoded (every byte except A-Z a-z 0-9 - _ ~ becomes %XX).
"""

from __future__ import annotations

import json
import re

LOUDNORM = "loudnorm=I=-16:TP=-1:LRA=9:print_format=json"
ANALYSIS_VERSION = "0.02"
FIELDS = {  # Plex key -> loudnorm output key
    "ln:gainOffset": "target_offset",
    "ln:loudness": "input_i",
    "ln:lra": "input_lra",
    "ln:peak": "input_tp",
    "ln:threshold": "input_thresh",
}
COMPARE_KEYS = ["ln:loudness", "ln:peak", "ln:lra", "ln:threshold", "ln:gainOffset"]
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def url_encode(text: str) -> str:
    out = []
    for byte in str(text).encode("utf-8"):
        char = chr(byte)
        if char.isascii() and (char.isalnum() or char in "-_~"):
            out.append(char)
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def build_extra(fields: dict, like: str = "") -> str:
    """Serialise extra_data the way Plex does.

    Plex is inconsistent about non-ASCII text (some rows hold raw UTF-8, others \\uXXXX
    escapes), so follow the style of the existing row given as `like`."""
    fields = {k: v for k, v in fields.items() if k != "url"}
    keys = sorted(fields)
    ordered = {k: fields[k] for k in keys}
    ordered["url"] = "&".join(f"{url_encode(k)}={url_encode(fields[k])}" for k in keys)
    raw_utf8 = any(ord(ch) > 127 for ch in like)
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=not raw_utf8)


def is_number(value) -> bool:
    """loudnorm prints -inf / inf for silence; Plex only stores real numbers."""
    return bool(_NUMBER.fullmatch(str(value or "")))


def loudness_fields(measure: dict) -> dict | None:
    """Plex fields from a loudnorm measurement, or None if it isn't usable."""
    if not measure or not all(is_number(measure.get(source)) for source in FIELDS.values()):
        return None
    fields = {key: measure[source] for key, source in FIELDS.items()}
    fields["ln:loudnessAnalysisVersion"] = ANALYSIS_VERSION
    return fields
