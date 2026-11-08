"""What's in Plex and what still needs loudness data: libraries, filters, progress."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .plexdb import PlexDB, sql_ids

MOVIE, EPISODE = 1, 4
HAS_LOUDNESS = "ms.extra_data LIKE '%ln:loudness%'"


@dataclass
class Filter:
    sections: list[int] = field(default_factory=list)  # library ids; empty = all video libraries
    added_since: int | None = None                     # unix time
    added_before: int | None = None
    year_from: int | None = None
    year_to: int | None = None
    items: list[int] = field(default_factory=list)     # metadata item ids
    include_done: bool = False                         # also tracks Plex already has data for
    limit: int = 0                                     # at most this many files
    newest_first: bool = True

    def describe(self) -> str:
        parts = []
        if self.added_since:
            parts.append(f"added since {dt.date.fromtimestamp(self.added_since)}")
        if self.added_before:
            parts.append(f"added before {dt.date.fromtimestamp(self.added_before)}")
        if self.year_from or self.year_to:
            parts.append(f"year {self.year_from or '…'}-{self.year_to or '…'}")
        if self.items:
            parts.append(f"{len(self.items)} chosen items")
        if self.include_done:
            parts.append("including analysed")
        if self.limit:
            parts.append(f"first {self.limit} files")
        return ", ".join(parts) or "everything not yet analysed"


_RELATIVE = re.compile(r"^(\d+)\s*([dwmy])$", re.I)


def parse_when(text: str) -> int | None:
    """'30d', '6w', '3m', '1y' (ago) or 'YYYY-MM-DD' -> unix time. Empty -> None."""
    text = (text or "").strip()
    if not text:
        return None
    match = _RELATIVE.match(text)
    if match:
        count, unit = int(match[1]), match[2].lower()
        days = count * {"d": 1, "w": 7, "m": 30.44, "y": 365.25}[unit]
        return int((dt.datetime.now() - dt.timedelta(days=days)).timestamp())
    try:
        return int(dt.datetime.strptime(text, "%Y-%m-%d").timestamp())
    except ValueError:
        raise ValueError(f"Use 30d / 6w / 3m / 1y or YYYY-MM-DD, not {text!r}") from None


def parse_years(text: str) -> tuple[int | None, int | None]:
    """'1990-1999', '2001', '2010-', '-1980' -> (from, to)."""
    text = (text or "").strip()
    if not text:
        return None, None
    match = re.fullmatch(r"(\d{4})?\s*(-)?\s*(\d{4})?", text)
    if not match or not (match[1] or match[3]):
        raise ValueError(f"Use a year like 2001 or a range like 1990-1999, not {text!r}")
    start = int(match[1]) if match[1] else None
    end = int(match[3]) if match[3] else (start if not match[2] else None)
    return start, end


def libraries(db: PlexDB) -> list[dict]:
    """Video libraries with item counts and how many already have loudness data."""
    return db.query(f"""
        SELECT ls.id, ls.name, ls.section_type AS type,
               COUNT(DISTINCT mei.id) AS items,
               COUNT(DISTINCT CASE WHEN {HAS_LOUDNESS} THEN mei.id END) AS done
        FROM library_sections ls
        JOIN metadata_items md ON md.library_section_id = ls.id AND md.metadata_type IN ({MOVIE}, {EPISODE})
        JOIN media_items mei ON mei.metadata_item_id = md.id AND mei.deleted_at IS NULL
        JOIN media_parts mp ON mp.media_item_id = mei.id AND mp.file != '' AND mp.deleted_at IS NULL
        JOIN media_streams ms ON ms.media_part_id = mp.id AND ms.stream_type_id = 2
        WHERE ls.section_type IN (1, 2)
        GROUP BY ls.id ORDER BY ls.name""")


def resolve_sections(db: PlexDB, names_or_ids: list[str]) -> list[int]:
    if not names_or_ids:
        return []
    known = {str(row["id"]): row["id"] for row in libraries(db)}
    known.update({row["name"].lower(): row["id"] for row in libraries(db)})
    out = []
    for value in names_or_ids:
        key = str(value).strip().lower()
        if key not in known:
            raise SystemExit(f"No video library {value!r}. Known: " + ", ".join(sorted(k for k in known if not k.isdigit())))
        out.append(known[key])
    return out


def _where(flt: Filter) -> str:
    year = "COALESCE(md.year, gp.year)"
    where = ["ms.stream_type_id = 2", "mp.file != ''", "mp.deleted_at IS NULL", "mei.deleted_at IS NULL",
             f"md.metadata_type IN ({MOVIE}, {EPISODE})", "ls.section_type IN (1, 2)"]
    if not flt.include_done:
        where.append(f"NOT ({HAS_LOUDNESS})")
    if flt.sections:
        where.append(f"md.library_section_id IN ({sql_ids(flt.sections)})")
    if flt.items:
        where.append(f"md.id IN ({sql_ids(flt.items)})")
    if flt.added_since:
        where.append(f"md.added_at >= {int(flt.added_since)}")
    if flt.added_before:
        where.append(f"md.added_at < {int(flt.added_before)}")
    if flt.year_from:
        where.append(f"{year} >= {int(flt.year_from)}")
    if flt.year_to:
        where.append(f"{year} <= {int(flt.year_to)}")
    return " AND ".join(where)


_FROM = """
    FROM media_streams ms
    JOIN media_parts mp ON mp.id = ms.media_part_id
    JOIN media_items mei ON mei.id = mp.media_item_id
    JOIN metadata_items md ON md.id = mei.metadata_item_id
    JOIN library_sections ls ON ls.id = md.library_section_id
    LEFT JOIN metadata_items p ON p.id = md.parent_id
    LEFT JOIN metadata_items gp ON gp.id = p.parent_id"""


def preview(db: PlexDB, flt: Filter) -> dict:
    """How much work a filter selects: files, audio tracks, hours of audio."""
    rows = db.query(f"""
        SELECT mp.id AS part, mei.duration AS duration, COUNT(*) AS tracks {_FROM}
        WHERE {_where(flt)} GROUP BY mp.id
        ORDER BY {'md.added_at DESC' if flt.newest_first else 'mp.file'}""")
    if flt.limit:
        rows = rows[: flt.limit]
    return {"files": len(rows), "tracks": sum(r["tracks"] for r in rows),
            "hours": sum((r["duration"] or 0) * r["tracks"] for r in rows) / 3_600_000}


def select_jobs(db: PlexDB, flt: Filter) -> list[dict]:
    """One job per media file, listing the audio streams that need measuring."""
    rows = db.query(f"""
        SELECT ms.id AS stream_id, ms."index" AS idx, ms.codec, ms.channels, ms.language,
               mp.id AS part_id, mp.file, mp.size, mei.duration, md.id AS item_id, md.title,
               md.added_at, COALESCE(md.year, gp.year) AS year, ls.name AS library,
               COALESCE(gp.title, '') AS show, p."index" AS season, md."index" AS episode
        {_FROM}
        WHERE {_where(flt)}
        ORDER BY {'md.added_at DESC' if flt.newest_first else 'mp.file'}, mp.id, ms."index" """)
    parts: dict[int, dict] = {}
    for row in rows:
        if row["part_id"] not in parts:
            if flt.limit and len(parts) >= flt.limit:
                continue
            if row["show"]:
                title = f"{row['show']} S{row['season'] or 0:02d}E{row['episode'] or 0:02d} {row['title'] or ''}"
            else:
                title = f"{row['title'] or ''} ({row['year']})" if row["year"] else (row["title"] or "")
            parts[row["part_id"]] = {
                "part_id": row["part_id"], "item_id": row["item_id"], "file": row["file"], "size": row["size"],
                "duration_ms": row["duration"] or 0, "library": row["library"], "title": title.strip(),
                "added_at": row["added_at"], "year": row["year"], "streams": [],
            }
        parts[row["part_id"]]["streams"].append(
            {"stream_id": row["stream_id"], "index": row["idx"], "codec": row["codec"],
             "channels": row["channels"], "language": row["language"]})
    return list(parts.values())


def stream_extra(db: PlexDB, stream_ids) -> dict[int, dict]:
    """Current extra_data and file size for the given audio streams."""
    rows = db.query(f"""
        SELECT ms.id, ms.extra_data, mp.size FROM media_streams ms
        JOIN media_parts mp ON mp.id = ms.media_part_id WHERE ms.id IN ({sql_ids(stream_ids)})""")
    return {row["id"]: row for row in rows}
