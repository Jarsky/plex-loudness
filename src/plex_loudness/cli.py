"""plex-loudness: the controller. Runs where Plex runs; queues work, shows progress, imports results.

Start the TUI with `plex-loudness tui` (or no arguments in a terminal), or use the commands below.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from . import __version__
from .analysis import Mode
from .batches import Queue
from .catalog import Filter, libraries, parse_when, parse_years, preview, resolve_sections, select_jobs
from .compare import compare, format_report, plex_reference
from .config import load
from .importer import apply_import, plan_import, selftest
from .plexdb import PlexDB


def human(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400 * 2:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds / 86400:.1f}d"


def filter_from_args(db: PlexDB, args) -> Filter:
    try:
        year_from, year_to = parse_years(args.year or "")
        return Filter(sections=resolve_sections(db, args.library or []), added_since=parse_when(args.added_since),
                      added_before=parse_when(args.added_before), year_from=year_from, year_to=year_to,
                      items=args.items or [], include_done=args.include_done, limit=args.limit,
                      newest_first=not args.oldest_first)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def load_records(queue: Queue, ref: str) -> tuple[dict[int, dict], str]:
    """Results from a batch id or a .jsonl results file."""
    path = Path(ref)
    if path.suffix == ".jsonl" and path.is_file():
        records = {}
        with open(path) as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                records[record["stream_id"]] = record
        return records, path.name
    batch = queue.get(ref)
    return batch.results(), f"{batch.id} ({batch.data.get('mode', {}).get('name', 'full')})"


# --------------------------------------------------------------------------- commands

def cmd_libraries(config, args) -> int:
    rows = libraries(PlexDB(config.plex))
    print(f"{'id':>4}  {'library':<28}{'type':<8}{'done':>8}{'items':>8}{'pct':>7}")
    for row in rows:
        pct = 100 * row["done"] / row["items"] if row["items"] else 0
        kind = "movies" if row["type"] == 1 else "shows"
        print(f"{row['id']:>4}  {row['name'][:27]:<28}{kind:<8}{row['done']:>8}{row['items']:>8}{pct:>6.1f}%")
    return 0


def print_batches(queue: Queue, limit: int = 15) -> None:
    batches = queue.batches()[-limit:]
    if not batches:
        print("No batches yet. Create one with: plex-loudness queue --library 'TV Shows'")
        return
    print(f"{'batch':<34}{'mode':<8}{'state':<10}{'tracks':>7}{'ok':>7}{'failed':>7}{'running':>8}{'audio h':>9}  filter")
    for batch in batches:
        s = batch.summary()
        print(f"{s['id'][:33]:<34}{s['mode'][:7]:<8}{s['state']:<10}{s['tracks']:>7}{s['ok']:>7}{s['failed']:>7}"
              f"{s['running']:>8}{s['hours_done']:>5.0f}/{s['hours']:<4.0f}  {s['filter'][:50]}")


def print_workers(queue: Queue) -> None:
    workers = queue.workers()
    if not workers:
        print("No workers have reported in yet.")
        return
    for w in workers:
        state = "alive" if w["alive"] else f"silent for {human(time.time() - w['updated'])}"
        print(f"worker {w['name']}: {state}, {len(w.get('active', []))}/{w.get('slots', '?')} busy, "
              f"{w.get('ok', 0)} ok / {w.get('failed', 0)} failed, {w.get('audio_hours', 0):.1f} h of audio, "
              f"{w.get('rate', 0):.0f}x real time, v{w.get('version', '?')}")


def cmd_status(config, args) -> int:
    queue = Queue(config.work_dir)
    if not args.no_plex:
        cmd_libraries(config, args)
        print()
    print_batches(queue)
    print()
    print_workers(queue)
    return 0


def cmd_queue(config, args) -> int:
    db = PlexDB(config.plex)
    flt = filter_from_args(db, args)
    try:
        mode = Mode.parse(args.mode, args.samples, args.sample_seconds)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    size = preview(db, flt)
    print(f"Selected: {size['files']} files, {size['tracks']} audio tracks, {size['hours']:,.0f} h of audio"
          f" ({flt.describe()}; mode {mode.name})")
    if args.dry_run or not size["tracks"]:
        return 0
    jobs = select_jobs(db, flt)
    names = {row["id"]: row["name"] for row in libraries(db)}
    name = args.name or (", ".join(names[s] for s in flt.sections) if flt.sections else "all libraries")
    batch = Queue(config.work_dir).create(name, jobs, mode.as_dict(), flt.describe(), created_by=os.uname().nodename)
    print(f"Queued batch {batch.id}. Workers pick it up within {config.worker.poll_seconds:g}s.")
    return 0


def cmd_batches(config, args) -> int:
    queue = Queue(config.work_dir)
    if args.action == "list":
        print_batches(queue, args.limit)
        return 0
    if not args.batch:
        raise SystemExit(f"batches {args.action} needs a batch id")
    batch = queue.get(args.batch)
    if args.action == "show":
        s = batch.summary()
        print(json.dumps({k: v for k, v in s.items()}, indent=2, default=str))
        failed = [r for r in batch.results().values() if r["status"] == "failed"]
        for record in failed[:20]:
            print(f"FAILED {record.get('file', '')}: {record.get('error', '')[:200]}")
    elif args.action == "cancel":
        batch.cancel()
        print(f"Cancelled {batch.id}: workers finish the tracks they're on and take no more.")
    elif args.action == "resume":
        batch.resume()
        print(f"Resumed {batch.id}")
    elif args.action == "retry":
        print(f"{batch.id}: {batch.retry_failed()} failed tracks will be tried again")
    elif args.action == "delete":
        if not args.yes:
            raise SystemExit("Deleting removes its results too. Add --yes to confirm.")
        queue.delete(batch)
        print(f"Deleted {batch.id}")
    return 0


def cmd_watch(config, args) -> int:
    queue = Queue(config.work_dir)
    while True:
        lines = [f"plex-loudness {__version__} | {dt.datetime.now():%H:%M:%S} | work dir {config.work_dir}", ""]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_batches(queue, 8)
            print()
            print_workers(queue)
        lines.append(buffer.getvalue())
        for w in queue.workers():
            if not w["alive"]:
                continue
            for a in w.get("active", []):
                pct = f"{100 * a['position'] / a['total']:.0f}%" if a.get("total") else "?"
                lines.append(f"  {w['name'][:10]:<11}{human(time.time() - a['started']):>8} {pct:>5} "
                             f"{a.get('rss_mb', 0):>5}MB  {a['title'][:60]} [{a['codec']}]")
        if args.once:
            print("\n".join(lines))
            return 0
        sys.stdout.write("\033[H\033[2J" + "\n".join(lines) + "\n(Ctrl+C to quit)\n")
        sys.stdout.flush()
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def cmd_import(config, args) -> int:
    db, queue = PlexDB(config.plex), Queue(config.work_dir)
    records, label = load_records(queue, args.source)
    plan = plan_import(db, list(records.values()), args.allow_sampled, args.overwrite)
    print(f"{label}: {plan.describe()}")
    if args.dry_run or not plan.statements:
        if plan.statements:
            print("Dry run. First change:\n  " + plan.statements[0][:300])
        return 0
    if not args.yes and sys.stdin.isatty():
        if input(f"Back up the database and write {plan.write} audio tracks into Plex? [y/N] ").lower() != "y":
            print("Cancelled")
            return 1
    result = apply_import(db, plan, backup=not args.no_backup)
    print(f"Verified: {result['verified']}/{result['written']} tracks now carry loudness data")
    if not args.source.endswith(".jsonl"):
        queue.get(args.source).mark_imported({"written": result["written"], "backup": result["backup"]})
    return 0 if result["verified"] == result["written"] else 1


def cmd_compare(config, args) -> int:
    queue = Queue(config.work_dir)
    test, label = load_records(queue, args.results)
    if args.plex or not args.reference:
        reference, ref_label = plex_reference(PlexDB(config.plex), list(test)), "Plex"
    else:
        reference, ref_label = load_records(queue, args.reference)
    result = compare(test, reference)
    if not result["tracks"]:
        raise SystemExit("No audio tracks in common to compare")
    print(format_report(result, label, ref_label))
    return 0


def cmd_selftest(config, args) -> int:
    ok, total, problems = selftest(PlexDB(config.plex), args.rows)
    for problem in problems:
        print("MISMATCH", problem)
    print(f"selftest: {ok}/{total} existing rows rebuilt byte for byte")
    return 0 if ok == total else 1


def cmd_backup(config, args) -> int:
    print(PlexDB(config.plex).backup())
    return 0


def cmd_config(config, args) -> int:
    print(f"# loaded from: {config.source or '(defaults, no config file found)'}")
    print(json.dumps({"work_dir": config.work_dir, "plex": vars(config.plex), "worker": vars(config.worker)}, indent=2))
    return 0


def cmd_tui(config, args) -> int:
    try:
        from .tui import run
    except ImportError as exc:
        raise SystemExit(f"The TUI needs Textual ({exc}). Install with: pip install 'plex-loudness[tui]'"
                         " or use the Docker image.") from None
    return run(config)


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plex-loudness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="config file (default: search, see README)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="interactive controller (default in a terminal)")
    sub.add_parser("libraries", help="video libraries and how much is analysed")
    p = sub.add_parser("status", help="libraries, batches and workers")
    p.add_argument("--no-plex", action="store_true", help="skip the database (batches and workers only)")

    p = sub.add_parser("queue", help="queue audio tracks for the workers")
    p.add_argument("--library", action="append", metavar="NAME|ID", help="library to include (repeatable)")
    p.add_argument("--added-since", metavar="WHEN", help="added to Plex since: 30d, 6w, 3m, 1y or YYYY-MM-DD")
    p.add_argument("--added-before", metavar="WHEN", help="added to Plex before (same formats)")
    p.add_argument("--year", metavar="RANGE", help="release year: 2001, 1990-1999, 2010-, -1980")
    p.add_argument("--items", type=int, nargs="+", metavar="ID", help="only these Plex item ids")
    p.add_argument("--limit", type=int, default=0, help="at most this many files")
    p.add_argument("--oldest-first", action="store_true", help="default order is newest additions first")
    p.add_argument("--include-done", action="store_true", help="also tracks Plex already has data for")
    p.add_argument("--mode", default="full", choices=["full", "quick", "sampled"],
                   help="full = exact (default); quick = 24x30 s samples, approximate; sampled = custom")
    p.add_argument("--samples", type=int, default=0)
    p.add_argument("--sample-seconds", type=float, default=30)
    p.add_argument("--name", help="batch name (default: library names)")
    p.add_argument("--dry-run", action="store_true", help="only show how much would be queued")

    p = sub.add_parser("batches", help="list or manage batches")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "cancel", "resume", "retry", "delete"])
    p.add_argument("batch", nargs="?", help="batch id (a unique prefix is enough)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("watch", help="live text view of batches and workers")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--once", action="store_true")

    p = sub.add_parser("import", help="write a batch's results into Plex (backs up first)")
    p.add_argument("source", help="batch id, or a results .jsonl file")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--overwrite", action="store_true", help="replace loudness data Plex already has")
    p.add_argument("--allow-sampled", action="store_true", help="also import approximate (quick/sampled) results")
    p.add_argument("--no-backup", action="store_true")

    p = sub.add_parser("compare", help="compare results with another batch, or with Plex's values")
    p.add_argument("results", help="batch id or results .jsonl")
    p.add_argument("reference", nargs="?", help="batch id or .jsonl to compare against (default: Plex)")
    p.add_argument("--plex", action="store_true", help="compare against Plex's stored values")

    p = sub.add_parser("selftest", help="check we can rebuild Plex's extra_data exactly")
    p.add_argument("--rows", type=int, default=20000)
    sub.add_parser("backup", help="online backup of the Plex database")
    sub.add_parser("config", help="show the effective configuration")
    sub.add_parser("worker", help="run a worker here (same as plex-loudness-worker)", add_help=False)
    return parser


COMMANDS = {"tui": cmd_tui, "libraries": cmd_libraries, "status": cmd_status, "queue": cmd_queue,
            "batches": cmd_batches, "watch": cmd_watch, "import": cmd_import, "compare": cmd_compare,
            "selftest": cmd_selftest, "backup": cmd_backup, "config": cmd_config}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "worker" in argv[:3]:
        from .worker import main as worker_main
        rest = list(argv)
        rest.remove("worker")
        return worker_main(rest)
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load(args.config)
    command = args.command or ("tui" if sys.stdin.isatty() and sys.stdout.isatty() else None)
    if not command:
        parser.print_help()
        return 1
    return COMMANDS[command](config, args)


if __name__ == "__main__":
    sys.exit(main())
