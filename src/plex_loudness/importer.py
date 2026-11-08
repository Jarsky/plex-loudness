"""Writing measurements into Plex, and checking our serialisation against Plex's."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .catalog import stream_extra
from .extradata import build_extra
from .plexdb import PlexDB, sql_ids, sql_text


@dataclass
class ImportPlan:
    statements: list[str] = field(default_factory=list)
    write: int = 0
    has_data: int = 0
    gone: int = 0
    changed_file: int = 0
    sampled_skipped: int = 0
    failed: int = 0
    stream_ids: list[int] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"{self.write:,} to write"]
        for count, text in ((self.has_data, "already have data"), (self.gone, "no longer in Plex"),
                            (self.changed_file, "file changed since measuring"),
                            (self.sampled_skipped, "sampled (not imported without --allow-sampled)"),
                            (self.failed, "failed measurements")):
            if count:
                bits.append(f"{count:,} {text}")
        return ", ".join(bits)


def plan_import(db: PlexDB, records: list[dict], allow_sampled=False, overwrite=False) -> ImportPlan:
    plan = ImportPlan()
    usable = []
    for record in records:
        if record.get("status") != "ok":
            plan.failed += 1
        elif record.get("mode", "full") != "full" and not allow_sampled:
            plan.sampled_skipped += 1
        else:
            usable.append(record)
    current = stream_extra(db, [r["stream_id"] for r in usable]) if usable else {}
    for record in usable:
        row = current.get(record["stream_id"])
        if not row:
            plan.gone += 1
            continue
        if record.get("size") is not None and row["size"] != record["size"]:
            plan.changed_file += 1
            continue
        old = row["extra_data"] or ""
        if "ln:loudness" in old and not overwrite:
            plan.has_data += 1
            continue
        fields = json.loads(old) if old else {}
        fields.update(record["ln"])
        guard = "" if overwrite else " AND (extra_data IS NULL OR extra_data NOT LIKE '%ln:loudness%')"
        plan.statements.append(f"UPDATE media_streams SET extra_data = {sql_text(build_extra(fields, like=old))}"
                               f" WHERE id = {int(record['stream_id'])}{guard};")
        plan.stream_ids.append(record["stream_id"])
        plan.write += 1
    return plan


def apply_import(db: PlexDB, plan: ImportPlan, backup=True, batch_size=200, progress=print) -> dict:
    """Back up the database, then write in small transactions and verify."""
    backup_path = db.backup() if backup and plan.statements else None
    for start in range(0, len(plan.statements), batch_size):
        chunk = plan.statements[start: start + batch_size]
        db.run(".timeout 30000\nBEGIN IMMEDIATE;\n" + "\n".join(chunk) + "\nCOMMIT;\n", readonly=False)
        progress(f"  committed {start + len(chunk)}/{len(plan.statements)}")
    verified = 0
    for start in range(0, len(plan.stream_ids), 500):
        ids = plan.stream_ids[start: start + 500]
        verified += db.query(f"SELECT count(*) AS n FROM media_streams WHERE id IN ({sql_ids(ids)})"
                             " AND extra_data LIKE '%ln:loudness%'")[0]["n"]
    return {"written": len(plan.statements), "verified": verified, "backup": backup_path}


def selftest(db: PlexDB, rows: int = 20000) -> tuple[int, int, list[str]]:
    """Rebuild existing extra_data rows and check they come out byte for byte."""
    sample = db.query("SELECT id, extra_data FROM media_streams WHERE extra_data LIKE '%\"url\":%'"
                      f" ORDER BY random() LIMIT {int(rows)}")
    problems = []
    for row in sample:
        original = row["extra_data"]
        if build_extra(json.loads(original), like=original) != original:
            problems.append(f"stream {row['id']}: {original[:200]}")
    return len(sample) - len(problems), len(sample), problems[:5]
